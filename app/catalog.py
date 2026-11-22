"""歌单目录绑定、实体副本与本地歌单；不依赖平台 ID 才能播放。"""

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from . import media
from .config import within
from .db import dumps, now
from .sources import exact_candidate, normalize


class Catalog:
    def bind_folder(self, pid, name=None):
        p = self.db.one("SELECT * FROM playlists WHERE id=?", (pid,))
        if not p:
            raise ValueError("歌单不存在")
        if p["folder"]:
            return p["folder"]
        folder = media.safe_name(name or p["name"])
        occupied = self.db.one("SELECT id FROM playlists WHERE folder=?", (folder,))
        if occupied and occupied["id"] != pid:
            folder += " [" + hashlib.sha256(pid.encode()).hexdigest()[:8] + "]"
        self.db.execute("UPDATE playlists SET folder=? WHERE id=?", (folder, pid))
        return folder

    def local_playlist(self, folder):
        if Path(folder).name != folder or folder.startswith((".", "@", "#")):
            raise ValueError("请选择音乐根目录下的文件夹")
        existing = self.db.one("SELECT id FROM playlists WHERE folder=?", (folder,))
        if existing:
            return existing["id"]
        pid = "local:" + hashlib.sha256(folder.encode()).hexdigest()[:20]
        self.db.execute(
            """INSERT OR IGNORE INTO playlists
            (id,platform,source_id,name,source_url,enabled,auto_add,snapshot,created,folder)
            VALUES (?,?,?,?,?,0,0,'[]',?,?)""",
            (pid, "local", pid, folder, "", now(), folder),
        )
        return pid

    def destination(self, pid=None):
        pid = pid or self.db.get("default_playlist")
        if not pid:
            pid = self.local_playlist("手动收藏")
        folder = self.bind_folder(pid)
        return pid, folder

    def flat_path(self, folder, track, ext):
        base = media.safe_name("、".join(track.get("artists") or ["未知歌手"]) + " - " + track["title"])
        relative = str(Path(folder) / (base + ext))
        p = within(self.config.music, relative)
        if p.exists() or self.db.one("SELECT id FROM assets WHERE path=?", (relative,)):
            suffix = hashlib.sha256(track["key"].encode()).hexdigest()[:10]
            relative = str(Path(folder) / (base + " [" + suffix + "]" + ext))
        return relative

    def queue_track(self, track, pid=None):
        pid, _ = self.destination(pid)
        key = self.canonical(track["key"])
        unique = "acquire:" + key
        # 同一歌曲的一次获取可服务多个歌单；领取前后的合并都在发布后重新读取。
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT id,payload FROM jobs WHERE unique_key=? AND state IN ('queued','running','paused')", (unique,)
            ).fetchone()
            if old:
                payload = json.loads(old["payload"])
                payload["playlist_ids"] = sorted(set(payload.get("playlist_ids", [])) | {pid})
                c.execute("UPDATE jobs SET payload=?,updated=? WHERE id=?", (dumps(payload), now(), old["id"]))
                return old["id"]
            jid = uuid.uuid4().hex
            c.execute(
                """INSERT INTO jobs(id,unique_key,kind,payload,title,state,created,updated)
                VALUES (?,?,'acquire',?,?,'queued',?,?)""",
                (jid, unique, dumps({"key": key, "playlist_ids": [pid]}), "获取 · " + track["title"], now(), now()),
            )
            return jid

    def copy_to_playlist(self, asset, pid):
        folder = self.bind_folder(pid)
        existing = self.db.one(
            "SELECT * FROM assets WHERE track_key=? AND present=1 AND substr(path,1,?)=?",
            (asset["track_key"], len(folder) + 1, folder + "/"),
        )
        if existing and within(self.config.music, existing["path"]).is_file():
            return existing
        source = within(self.config.music, asset["path"])
        if not source.is_file() or source.stat().st_size != asset["size"]:
            raise ValueError("原文件已变化，请重新扫描")
        tags = json.loads(asset["data"])
        track = self.db.track(asset["track_key"]) or {**tags, "key": asset["track_key"] or "local:" + asset["id"]}
        digest = media.sha(source)
        base = media.safe_name("、".join(track.get("artists") or ["未知歌手"]) + " - " + track["title"])
        candidate = within(self.config.music, str(Path(folder) / (base + source.suffix)))
        relative = (
            str(candidate.relative_to(self.config.music))
            if candidate.is_file() and media.sha(candidate) == digest
            else self.flat_path(folder, track, source.suffix)
        )
        target = within(self.config.music, relative)
        target.parent.mkdir(exist_ok=True)
        digest = media.sha(source)
        # 崩溃后遇到同名文件只接受完全一致的内容；从不覆盖冲突。
        if target.exists() and media.sha(target) != digest:
            raise ValueError("歌单目录中已有不同的同名文件，待核对")
        if not target.exists():
            temp = target.with_name("." + target.name + ".musicprep-pending")
            shutil.copyfile(source, temp)
            if media.sha(temp) != digest:
                raise ValueError("歌单副本校验失败")
            os.replace(temp, target)
        side = source.with_suffix(".lrc")
        if side.is_file():
            dest = target.with_suffix(".lrc")
            if dest.exists() and dest.read_bytes() != side.read_bytes():
                raise ValueError("已有不同歌词，待核对")
            if not dest.exists():
                media.atomic_bytes(dest, side.read_bytes())
        aid = uuid.uuid4().hex
        stat = target.stat()
        self.db.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, relative, asset["track_key"], dumps(tags), stat.st_size, stat.st_mtime_ns, digest, 1, now()),
        )
        cover = self.config.data / "covers" / (asset["id"] + ".jpg")
        if cover.exists():
            shutil.copyfile(cover, self.config.data / "covers" / (aid + ".jpg"))
        return self.db.one("SELECT * FROM assets WHERE id=?", (aid,))

    def distribute(self, jid, result):
        job = self.db.one("SELECT payload FROM jobs WHERE id=?", (jid,))
        payload = json.loads(job["payload"])
        asset = self.db.one("SELECT * FROM assets WHERE id=?", (result["asset_id"],))
        delivered = set(result.get("delivered_to", []))
        for pid in payload.get("playlist_ids", []):
            self.check(jid)
            self.copy_to_playlist(asset, pid)
            delivered.add(pid)
            p = self.db.one('SELECT snapshot FROM playlists WHERE id=?', (pid,))
            if p:
                with self.db.connect() as c:
                    for track in json.loads(p['snapshot']):
                        if self.canonical(track['key']) == asset['track_key']:
                            c.execute('INSERT OR IGNORE INTO subscription_acquired VALUES(?,?)', (pid, track['key']))
                            c.execute("DELETE FROM track_matches WHERE original=?", (track['key'],))
        result["delivered_to"] = sorted(delivered)
        self.export_all()
        return result

    def finish_distribution(self, jid, result):
        while True:
            result = self.distribute(jid, result)
            self.check(jid)
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                latest = c.execute("SELECT payload FROM jobs WHERE id=?", (jid,)).fetchone()
                wanted = set(json.loads(latest["payload"]).get("playlist_ids", []))
                if wanted <= set(result.get("delivered_to", [])):
                    c.execute(
                        "UPDATE jobs SET state='succeeded',stage='已完成',progress=100,result=?,error=NULL,updated=? WHERE id=?",
                        (dumps(result), now(), jid),
                    )
                    return

    def queue_enrichment(self, assets):
        ids = []
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            active = {
                r["unique_key"]: r["id"] for r in c.execute("SELECT id,unique_key FROM jobs WHERE state IN ('queued','running','paused')")
            }
            for a in assets:
                tags = json.loads(a["data"])
                if not tags.get("missing") or tags.get("locked"):
                    continue
                key = "enrich:" + a["id"]
                if key in active:
                    ids.append(active[key])
                    continue
                jid = uuid.uuid4().hex
                c.execute(
                    "INSERT INTO jobs(id,unique_key,kind,payload,title,state,created,updated) VALUES (?,?,'enrich',?,?,'queued',?,?)",
                    (jid, key, dumps({"asset_id": a["id"]}), "补齐 · " + tags["title"], now(), now()),
                )
                ids.append(jid)
        return ids

    def folder_assets(self, folder):
        return self.db.rows(
            "SELECT * FROM assets WHERE present=1 AND substr(path,1,?)=? ORDER BY path COLLATE NOCASE", (len(folder) + 1, folder + "/")
        )

    def playlist_tracks(self, p):
        if p["platform"] == "local":
            return [
                {
                    **json.loads(a["data"]),
                    "key": a["track_key"] or "local:" + a["id"],
                    "asset_id": a["id"],
                    "available": True,
                    "path": a["path"],
                }
                for a in self.folder_assets(p["folder"])
            ]
        assets = self.db.rows("SELECT id,path,track_key FROM assets WHERE present=1")
        by_key = {}
        for a in assets:
            if a["track_key"] not in by_key or a["path"].startswith((p["folder"] or "\0") + "/"):
                by_key[a["track_key"]] = a
        mappings = {r["original"]: r["target"] for r in self.db.rows("SELECT * FROM mappings")}
        result = []
        for t in json.loads(p["snapshot"]):
            key, seen = t["key"], set()
            while key in mappings and key not in seen:
                seen.add(key)
                key = mappings[key]
            a = by_key.get(key)
            result.append({**t, "asset_id": a["id"] if a else None, "available": bool(a), "path": a["path"] if a else None})
        return result

    def match_asset(self, asset):
        data = json.loads(asset["data"])
        original = self.db.track(asset["track_key"]) if asset["track_key"] else None
        if original and original.get("platform") != "local":
            return original
        if not data.get("artists") or not data.get("duration"):
            return None
        candidates = self.sources.search((data["title"] + " " + " ".join(data["artists"]))[:160], "netease")
        matches = [t for t in candidates if exact_candidate(data, t)]
        if len(matches) > 1 and data.get("album"):
            matches = [t for t in matches if normalize(t.get("album", "")) == normalize(data["album"])]
        if len(matches) != 1:
            return None
        t = matches[0]
        self.db.save_track(t)
        self.db.execute("UPDATE assets SET track_key=? WHERE id=?", (t["key"], asset["id"]))
        asset["track_key"] = t["key"]
        return t
