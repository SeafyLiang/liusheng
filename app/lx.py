"""私有 LX 音源管理，验证结果与导入状态分开保存。"""

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

import httpx

from . import media
from .db import dumps, now
from .public_http import fetch_public, open_public
from .sources import SourceError

LIMIT = 256 * 1024
PLATFORMS = {"netease": "wy", "tencent": "tx", "kuwo": "kw"}
QUALITIES = ("flac24bit", "flac", "320k", "128k")
ERRORS = {
    "init_failed": "脚本未声明有效的平台能力", "script_error": "脚本执行失败或使用了尚未兼容的接口",
    "script_rejected": "音源未能解析这首歌", "timeout": "音源执行超时", "resource_limit": "脚本超过执行资源限制",
    "busy": "音源执行器正在忙，请稍后重试", "runner_error": "音源执行器暂不可用", "missing_handler": "脚本未注册歌曲解析函数",
}


class LXManager:
    def __init__(self, cfg, db):
        self.db, self.cfg = db, cfg
        self.directory = cfg.data / "lx-sources"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.Lock()
        db.execute("""CREATE TABLE IF NOT EXISTS lx_sources (
            id TEXT PRIMARY KEY, metadata TEXT NOT NULL, capabilities TEXT NOT NULL,
            state TEXT NOT NULL, detail TEXT NOT NULL, checked INTEGER, created INTEGER NOT NULL,
            verified_platforms TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '{}')""")

    def items(self):
        providers = self.db.get("providers", ["gd", "netease_direct"])
        return [{**r, **json.loads(r["metadata"]), "metadata": None,
                 "capabilities": json.loads(r["capabilities"]), "verified_platforms": json.loads(r["verified_platforms"]),
                 "evidence": json.loads(r["evidence"]), "enabled": "lx:" + r["id"] in providers}
                for r in self.db.rows("SELECT * FROM lx_sources ORDER BY created DESC,id")]

    def item(self, ident):
        if not re.fullmatch(r"[a-f0-9]{64}", ident):
            raise ValueError("音源编号无效")
        row = self.db.one("SELECT * FROM lx_sources WHERE id=?", (ident,))
        if not row:
            raise ValueError("音源不存在")
        return row

    def run(self, ident, request=None):
        row = self.item(ident)
        endpoint = os.getenv("LX_RUNNER_URL", "")
        token = os.getenv("LX_RUNNER_TOKEN", "")
        if not endpoint or not token:
            raise SourceError("LX 执行服务尚未配置", "runner_unavailable")
        script = (self.directory / (ident + ".js")).read_text()
        if hashlib.sha256(script.encode()).hexdigest() != ident:
            raise SourceError("脚本内容已变化，请重新导入", "integrity")
        try:
            with httpx.Client(timeout=40, trust_env=False) as client:
                r = client.post(endpoint.rstrip("/") + "/run", headers={"X-Runner-Token": token},
                                json={"script": script, "metadata": json.loads(row["metadata"]), "request": request})
            if r.status_code == 429:
                raise SourceError(ERRORS["busy"], "busy")
            r.raise_for_status()
            result = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SourceError("LX 执行服务连接失败", "runner_unavailable") from e
        return result

    def import_url(self, url):
        try:
            status, _, data = fetch_public(url, limit=LIMIT)
        except (ValueError, OSError) as e:
            raise SourceError("脚本链接无法读取，或域名解析到了受限地址；可改用 JS 文件导入", "import_failed") from e
        if status != 200:
            raise SourceError(f"脚本链接返回 HTTP {status}", "import_failed")
        return self.import_script(data)

    def import_script(self, data):
        if not 0 < len(data) <= LIMIT:
            raise ValueError("脚本文件须在 256 KiB 以内")
        try:
            script = data.decode("utf-8-sig")
        except UnicodeError as e:
            raise ValueError("请导入 UTF-8 编码的 JS 文件") from e
        if script.lstrip().startswith("<") or not re.search(r"@name\s+", script[:4096]):
            raise ValueError("文件不是带 @name 声明的 LX 音源脚本")
        metadata = {}
        for key in ("name", "version", "description", "author", "homepage"):
            match = re.search(r"@" + key + r"[^\S\n]+([^\r\n]*)", script[:4096])
            metadata[key] = match[1].strip().rstrip("*/").strip()[:300] if match else ""
        metadata["batch_restricted"] = bool(re.search(r"禁止.{0,4}批量|禁止.{0,4}下载|no\s+batch", script[:4096], re.I))
        ident = hashlib.sha256(script.encode()).hexdigest()
        if self.db.one("SELECT id FROM lx_sources WHERE id=?", (ident,)):
            return {"id": ident, "duplicate": True}
        path = self.directory / (ident + ".js")
        with path.open("w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(script)
        self.db.execute("INSERT INTO lx_sources(id,metadata,capabilities,state,detail,created) VALUES(?,?,'{}','imported',?,?)",
                        (ident, dumps(metadata), "脚本已保存，等待兼容性检查与歌曲验证", now()))
        return {"id": ident, "duplicate": False}

    def inspect(self, ident):
        try:
            result = self.run(ident)
            capabilities = result.get("capabilities", {}) if result.get("ok") else {}
            capabilities = {k: {"qualitys": [q for q in v.get("qualitys", []) if q in QUALITIES],
                                "actions": [a for a in v.get("actions", []) if a in ("musicUrl", "lyric", "pic")]}
                            for k, v in capabilities.items() if k in ("wy", "tx", "kw", "kg", "mg", "local") and isinstance(v, dict)}
            if not capabilities:
                raise SourceError(ERRORS.get(result.get("code"), "脚本没有声明可用能力"), result.get("code") or "init_failed")
            # 重新检查不擦除已验证证据。
            self.db.execute("UPDATE lx_sources SET capabilities=?,state=CASE WHEN state='verified' THEN state ELSE 'ready' END,detail=?,checked=? WHERE id=?",
                            (dumps(capabilities), "初始化通过；整曲可用性以歌曲验证记录为准", now(), ident))
            return {"ok": True, "capabilities": capabilities}
        except SourceError as e:
            self.fail(ident, e)
            return {"ok": False, "code": e.code, "detail": str(e)}

    def fail(self, ident, error, evidence=None):
        # 单个平台的失败不能抹掉其他平台已完成的整曲验证；初始化失败才清空全部。
        remaining = []
        if evidence and evidence.get("track"):
            failed_platform = PLATFORMS.get(evidence["track"].split(":", 1)[0])
            remaining = [p for p in json.loads(self.item(ident)["verified_platforms"]) if p != failed_platform]
        detail = str(error)[:230] + ("；其他已验证平台仍可使用" if remaining else "")
        self.db.execute("UPDATE lx_sources SET state=?,detail=?,checked=?,verified_platforms=?,evidence=? WHERE id=?",
                        ("verified" if remaining else "failed", detail, now(), dumps(remaining), dumps(evidence or {}), ident))
        if not remaining:
            self.db.set("providers", [p for p in self.db.get("providers", ["gd", "netease_direct"]) if p != "lx:" + ident])

    def resolve(self, ident, track, bitrate=999, testing=False):
        row = self.item(ident)
        metadata = json.loads(row["metadata"])
        platform = PLATFORMS.get(track["platform"])
        if not platform or platform not in json.loads(row["capabilities"]):
            raise SourceError("该脚本不支持所选歌曲的平台", "unsupported")
        if not testing and (metadata.get("batch_restricted") or row["state"] != "verified" or platform not in json.loads(row["verified_platforms"])):
            raise SourceError("该音源未通过此平台验证，或不允许任务批量获取", "not_verified")
        health = self.db.one("SELECT blocked_until FROM source_health WHERE source=?", ("lx:" + ident,))
        if health and health["blocked_until"] > now():
            raise SourceError("音源暂时冷却，请稍后重试", "cooldown")
        qualities = json.loads(row["capabilities"])[platform].get("qualitys", [])
        preferences = tuple(reversed(QUALITIES)) if bitrate == 128 else QUALITIES
        quality = next((q for q in preferences if q in qualities), None)
        if not quality:
            raise SourceError("脚本未声明兼容音质", "unsupported")
        music = {"songmid": track["id"], "songId": track.get("song_id", track["id"]),
                 "strMediaMid": track.get("media_mid", track["id"]), "name": track["title"], "songname": track["title"],
                 "singer": "、".join(track["artists"]), "albumName": track.get("album", ""), "interval": track.get("duration", 0), "source": platform}
        result = self.run(ident, {"action": "musicUrl", "source": platform, "info": {"type": quality, "musicInfo": music}})
        if not result.get("ok") or not isinstance(result.get("url"), str) or not result["url"].startswith(("https://", "http://")):
            statuses = [r.get("status") for r in result.get("requests", [])]
            code = "auth_required" if any(s in (401, 403) for s in statuses) else "rate_limited" if 429 in statuses else result.get("code") or "invalid_url"
            detail = "上游拒绝访问或需要授权" if code == "auth_required" else "上游限流" if code == "rate_limited" else ERRORS.get(code, "音源没有返回有效音频地址")
            if statuses:
                detail += "（HTTP " + "/".join(str(s) for s in statuses if s is not None) + "）"
            self.db.execute("INSERT INTO source_health VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET status=excluded.status,detail=excluded.detail,checked=excluded.checked,blocked_until=excluded.blocked_until",
                            ("lx:" + ident, code, detail, now(), now() + (300 if code == "rate_limited" else 60)))
            raise SourceError(detail, code)
        return {"url": result["url"], "quality": quality, "public_media": True}

    def probe(self, ident, track):
        if not self.lock.acquire(blocking=False):
            raise SourceError("已有音源正在验证，请等待完成", "busy")
        try:
            if track.get("duration", 0) <= 0:
                raise ValueError("验证歌曲需要可靠时长")
            if not self.inspect(ident)["ok"]:
                return {"ok": False, "detail": self.item(ident)["detail"]}
            evidence = {"track": track["key"], "title": track["title"], "expected_duration": track["duration"], "checked": now()}
            try:
                result = self.resolve(ident, track, bitrate=128, testing=True)
                evidence["resolved"] = True
                with tempfile.TemporaryDirectory(prefix="lx-probe-", dir=self.cfg.data / "staging") as directory:
                    target = Path(directory) / "audio.part"
                    download_public(result["url"], target, max_bytes=64 * 1024 * 1024)
                    info = media.probe(target, track["duration"])
                    evidence.update(duration=info["duration"], codec=info["codec_name"], bytes=target.stat().st_size, sha256=media.sha(target), full_decode=True)
                row = self.item(ident)
                platforms = sorted(set(json.loads(row["verified_platforms"])) | {PLATFORMS[track["platform"]]})
                self.db.execute("UPDATE lx_sources SET state='verified',detail=?,checked=?,verified_platforms=?,evidence=? WHERE id=?",
                                ("样本整曲下载、时长和完整解码通过；未自动入库", now(), dumps(platforms), dumps(evidence), ident))
                return {"ok": True, "evidence": evidence}
            except Exception as e:
                if isinstance(e, SourceError):
                    error = e
                else:
                    error = SourceError("解析后未通过完整音频检查" if evidence.get("resolved") else "音源验证失败", "invalid_audio")
                self.fail(ident, error, evidence)
                return {"ok": False, "detail": str(error), "code": error.code, "evidence": evidence}
        finally:
            self.lock.release()

    def eligible(self):
        return ["lx:" + x["id"] for x in self.items() if x["state"] == "verified" and not x["batch_restricted"]]


def download_public(url, target, cancel=lambda: None, max_bytes=512 * 1024 * 1024):
    response = connection = None
    total = 0
    try:
        response, connection = open_public(url, timeout=20)
        if response.status != 200:
            raise SourceError(f"音频服务返回 HTTP {response.status}", "auth_required" if response.status in (401, 403) else "unavailable")
        length = int(response.getheader("Content-Length") or 0)
        if length > max_bytes:
            raise SourceError("音频超过大小限制", "too_large")
        with target.open("wb") as f:
            while True:
                cancel()
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SourceError("音频超过大小限制", "too_large")
                f.write(chunk)
        if length and total != length:
            raise SourceError("音频下载不完整", "incomplete")
        return total
    except (OSError, ValueError) as e:
        raise SourceError("音频链接无法安全读取", "invalid_url") from e
    finally:
        if connection:
            connection.close()
