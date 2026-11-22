"""仅为已登录用户准备可丢弃的试听/兼容副本，不写曲库与获取任务。"""

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from . import media
from .config import within
from .sources import SourceError

MIB = 1024 * 1024
MIME = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav", ".ape": "audio/x-ape"}


class Playback:
    limit = 384 * MIB
    ttl = 2 * 3600

    def __init__(self, cfg, db, sources):
        self.cfg, self.db, self.sources = cfg, db, sources
        self.root = cfg.data / "playback-cache"
        self.root.mkdir(exist_ok=True)
        # 此目录仅含应用生成的缓存；重启后旧 token 作废。
        for p in self.root.iterdir():
            if p.is_file() and p.suffix in (".mp3", ".part"):
                p.unlink()
        self.lock = threading.RLock()
        self.entries = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview")
        self.closing = threading.Event()

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def asset(self, ident):
        a = self.db.one("SELECT * FROM assets WHERE id=? AND present=1", (ident,))
        if not a:
            raise HTTPException(404, "歌曲文件不在音乐库中")
        path = within(self.cfg.music, a["path"])
        if not path.is_file() or path.suffix.lower() not in MIME:
            raise HTTPException(404, "歌曲文件已移动，请重新扫描音乐库")
        return a, path

    def clean(self, reserve=0):
        now = time.time()
        total = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        for ident, e in sorted(list(self.entries.items()), key=lambda pair: pair[1]["used"]):
            if e["state"] == "preparing" or now - e["used"] < 120:
                continue
            if now - e["used"] > self.ttl or total + reserve > self.limit or len(self.entries) > 60:
                p = self.root / (ident + ".mp3")
                if p.exists():
                    total -= p.stat().st_size
                    p.unlink()
                del self.entries[ident]
        if total + reserve > self.limit:
            raise HTTPException(429, "试听缓存暂满，请稍后再试；已下载歌曲仍可直接播放")

    def public(self, ident):
        with self.lock:
            e = self.entries.get(ident)
            if not e:
                raise HTTPException(404, "试听已过期，请重新点击试听")
            e["used"] = time.time()
            return {k: v for k, v in e.items() if k != "used"} | {"id": ident}

    def prepare(self, track=None, asset_id=None):
        if asset_id:
            a, path = self.asset(asset_id)
            st = path.stat()
            key = f"asset:{asset_id}:{st.st_mtime_ns}:{st.st_size}"
            metadata = json.loads(a["data"])
        else:
            key, metadata, path = "preview:" + track["key"], track, None
        ident = hashlib.sha256(key.encode()).hexdigest()[:32]
        with self.lock:
            e = self.entries.get(ident)
            if e and (e["state"] == "preparing" or (e["state"] == "ready" and (self.root / (ident + ".mp3")).is_file())):
                return self.public(ident)
            self.clean(128 * MIB)
            if len(self.entries) >= 128 and ident not in self.entries:
                raise HTTPException(429, "试听请求较多，请稍后重试")
            if any(e["state"] == "preparing" for e in self.entries.values()):
                raise HTTPException(429, "正在准备另一首试听，请稍后再试")
            self.entries[ident] = {"state": "preparing", "used": time.time(), "label": "正在准备音频", "attempts": []}
            self.pool.submit(self.build, ident, metadata, path)
            return self.public(ident)

    def build(self, ident, track, local):
        raw, output = self.root / (ident + ".part"), self.root / (ident + ".mp3")
        deadline = time.monotonic() + 150

        def check():
            if self.closing.is_set() or time.monotonic() > deadline:
                raise SourceError("准备超时或服务正在重启，请重试", "timeout")

        attempts = []
        try:
            providers = ["local"] if local else self.db.get("providers", ["gd", "netease_direct"])
            for provider in providers:
                try:
                    check()
                    if local:
                        source = local
                    else:
                        resolved = self.sources.audio_url(track, provider, bitrate=128)
                        if resolved.get("public_media"):
                            from .lx import download_public
                            download_public(resolved["url"], raw, check, max_bytes=64 * MIB)
                        else:
                            self.sources.download(resolved["url"], raw, check, max_bytes=64 * MIB)
                        source = raw
                    info = media.probe(source, decode=False)
                    if info["duration"] > 3600:
                        raise SourceError("这首音频超过一小时，请使用原文件播放", "too_long")
                    check()
                    if info["codec_name"] == "mp3" and not local:
                        raw.replace(output)
                    else:
                        # 临时兼容副本仅按需生成，单线程、时长/输出/执行时间均受限。
                        media.command(
                            [
                                "ffmpeg",
                                "-nostdin",
                                "-v",
                                "error",
                                "-threads",
                                "1",
                                "-i",
                                str(source),
                                "-map",
                                "0:a:0",
                                "-vn",
                                "-map_metadata",
                                "-1",
                                "-c:a",
                                "libmp3lame",
                                "-threads",
                                "1",
                                "-b:a",
                                "192k",
                                "-fs",
                                str(64 * MIB),
                                "-y",
                                str(output),
                            ],
                            timeout=90,
                        )
                        if output.stat().st_size >= 64 * MIB:
                            raise SourceError("兼容副本超过大小上限", "too_large")
                    result = media.probe(output, expected=info["duration"], decode=False)
                    expected = track.get("duration") or 0
                    partial = bool(expected and abs(result["duration"] - expected) > max(3, expected * 0.02))
                    attempts.append({"source": provider, "status": "ready"})
                    with self.lock:
                        self.entries[ident].update(
                            state="ready",
                            used=time.time(),
                            duration=result["duration"],
                            partial=partial,
                            source=provider,
                            attempts=attempts,
                            label="兼容播放" if local else "试听片段 / 时长不符" if partial else "在线试听",
                            url=f"/api/playback/cache/{ident}",
                        )
                    return
                except Exception as exc:
                    output.unlink(missing_ok=True)
                    attempts.append(
                        {
                            "source": provider,
                            "status": getattr(exc, "code", "unavailable"),
                            "detail": str(exc) if isinstance(exc, SourceError) else "来源音频不可读取或转换失败",
                        }
                    )
            with self.lock:
                self.entries[ident].update(state="failed", label="暂时无法准备音频，请重试或切换版本", attempts=attempts)
        finally:
            raw.unlink(missing_ok=True)

    def cached(self, ident):
        with self.lock:
            result = self.public(ident)
            path = self.root / (ident + ".mp3")
            if result["state"] != "ready" or not path.is_file():
                raise HTTPException(404, "试听已过期或尚未准备好")
            return path
