import json
import logging
import os
import shutil
import sqlite3
import threading
import unicodedata
import uuid
from pathlib import Path

from . import media
from .catalog import Catalog
from .config import within
from .db import dumps, now
from .sources import SourceError
from .task_control import rate_status
from .subscriptions import next_check, validate_options, merge_members

logger = logging.getLogger(__name__)


class Interrupted(Exception):
    pass


def snapshot_diff(old, new):
    a = [t["key"] for t in old]
    b = [t["key"] for t in new]
    sa, sb = set(a), set(b)
    return {
        "added": [t for t in new if t["key"] not in sa],
        "removed": [t for t in old if t["key"] not in sb],
        "reordered": [x for x in a if x in sb] != [x for x in b if x in sa],
    }


class Engine(Catalog):
    def __init__(self, config, db, sources):
        self.config, self.db, self.sources = config, db, sources
        self.stop = threading.Event()
        self.thread = None
        self.heartbeat = now()
        self._lockfile = None
        self.worker_failures = 0
        self.worker_last_error = None
        self.worker_last_error_at = None
        self.worker_phase = "starting"
        self._recovery_job_id = None

    def start(self):
        import fcntl

        self._lockfile = (self.config.data / "worker.lock").open("w")
        fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db.execute("UPDATE jobs SET state='queued',stage='重启后恢复',updated=? WHERE state='running'", (now(),))
        self.db.execute("UPDATE jobs SET timing_partial=1,run_started_at=NULL WHERE run_started_at IS NOT NULL")
        self.db.execute("UPDATE job_runs SET outcome='interrupted' WHERE outcome IS NULL")
        self.thread = threading.Thread(target=self.loop, name="music-worker", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=35)
        if self._lockfile and (not self.thread or not self.thread.is_alive()):
            self._lockfile.close()

    def enqueue(self, kind, payload, title, unique_key=None):
        key = unique_key or f"{kind}:{payload.get('key') or payload.get('asset_id') or payload.get('playlist_id') or 'global'}"
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute("SELECT id FROM jobs WHERE unique_key=? AND state IN ('queued','running','paused')", (key,)).fetchone()
            if prior:
                return prior["id"]
            jid = uuid.uuid4().hex
            c.execute(
                "INSERT INTO jobs (id,unique_key,kind,payload,title,state,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                (jid, key, kind, dumps(payload), title, "queued", now(), now()),
            )
            return jid

    def update(self, jid, stage, progress):
        self.db.execute("UPDATE jobs SET stage=?,progress=?,updated=? WHERE id=?", (stage, progress, now(), jid))
        self.heartbeat = now()

    def check(self, jid):
        if self.stop.is_set():
            raise Interrupted("服务停止，稍后恢复")
        j = self.db.one("SELECT cancel_requested FROM jobs WHERE id=?", (jid,))
        if j and j["cancel_requested"]:
            raise Interrupted("已暂停" if j["cancel_requested"] == 2 else "已取消")

    def attempt(self, jid, source, status, detail=""):
        self.db.execute(
            "INSERT INTO attempts(job_id,source,status,detail,created) VALUES (?,?,?,?,?)", (jid, source, status, detail[:400], now())
        )

    def health_status(self):
        alive = bool(self.thread and self.thread.is_alive())
        state = "retrying" if self.worker_failures else "running"
        return {
            "worker": alive,
            "worker_state": state if alive else "stopped",
            "worker_phase": self.worker_phase,
            "worker_last_activity_at": self.heartbeat,
            "worker_last_error": self.worker_last_error,
            "worker_last_error_at": self.worker_last_error_at,
            "worker_retry_count": self.worker_failures,
        }

    def _recover_job(self, job_id):
        clock = now()
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return
            state = job["state"]
            if state == "running":
                state = {1: "cancelled", 2: "paused"}.get(job["cancel_requested"], "queued")
                connection.execute(
                    "UPDATE jobs SET state=?,stage='异常后恢复',next_run=max(next_run,?),updated=? WHERE id=?",
                    (state, clock + 30, clock, job_id),
                )
            elapsed = max(0, clock - job["run_started_at"]) if job["run_started_at"] is not None else 0
            finished = clock if state in ("succeeded", "failed", "cancelled", "review") else None
            connection.execute(
                "UPDATE jobs SET elapsed_seconds=elapsed_seconds+?,run_started_at=NULL,"
                "finished_at=coalesce(finished_at,?),timing_partial=1 WHERE id=?",
                (elapsed, finished, job_id),
            )
            outcome = "interrupted" if state == "queued" else state
            connection.execute(
                "UPDATE job_runs SET finished=?,outcome=? WHERE job_id=? AND outcome IS NULL",
                (clock, outcome, job_id),
            )

    def loop(self):
        last_schedule = 0
        while not self.stop.is_set():
            job = None
            try:
                self.heartbeat = now()
                if self._recovery_job_id:
                    self.worker_phase = "recovering"
                    self._recover_job(self._recovery_job_id)
                    self._recovery_job_id = None
                if now() - last_schedule >= 30:
                    self.worker_phase = "scheduling"
                    self.schedule()
                    last_schedule = now()
                self.worker_phase = "queue"
                if not self.db.get("queue_paused", False) and rate_status(self.db, now())["wait_seconds"] == 0:
                    job = self.db.one("SELECT * FROM jobs WHERE state='queued' AND next_run<=? ORDER BY CASE WHEN kind='sync' AND json_extract(payload,'$.weekly_due')=1 THEN -1 WHEN kind IN ('acquire','import') THEN 0 WHEN kind IN ('scan','sync') THEN 1 ELSE 2 END,created,id LIMIT 1", (now(),))
                if self.worker_failures:
                    logger.info("后台任务循环已恢复")
                self.worker_failures = 0
                if job:
                    self.worker_phase = "processing"
                    self.run_job(job)
                    continue
                self.worker_phase = "idle"
                self.stop.wait(1)
            except Exception as error:
                if job is not None:
                    self._recovery_job_id = job["id"]
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                busy = isinstance(error, sqlite3.OperationalError) and (
                    code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    or str(error).lower().startswith(("database is locked", "database table is locked", "database schema is locked"))
                )
                category = "database_busy" if busy else type(error).__name__
                previous_error = self.worker_last_error
                self.worker_failures += 1
                self.worker_last_error = category
                self.worker_last_error_at = now()
                delay = min(30, 2 ** min(self.worker_failures - 1, 5))
                if self.worker_failures == 1 or category != previous_error:
                    logger.warning("后台任务循环异常（%s，%s），%s 秒后重试", self.worker_phase, category, delay)
                self.stop.wait(delay)

    def schedule(self):
        for p in self.db.rows("SELECT id,name,schedule FROM playlists WHERE enabled=1 AND platform!='local' AND schedule!='manual' AND next_check<=?", (now(),)):
            self.enqueue("sync", {"playlist_id": p["id"], "weekly_due": p["schedule"] == "weekly_monday_0400"}, "检查歌单 · " + p["name"])
        self.db.execute("DELETE FROM sessions WHERE expires<?", (now(),))
        self.db.execute("DELETE FROM previews WHERE expires<?", (now(),))

    def run_job(self, job):
        jid = job["id"]
        began = now()
        with self.db.connect() as c:
            if not c.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,error=NULL,started_at=coalesce(started_at,?),run_started_at=?,finished_at=NULL,updated=? WHERE id=? AND state='queued'",
                (began, began, began, jid),
            ).rowcount:
                return
            run_id = c.execute('INSERT INTO job_runs(job_id,started) VALUES (?,?)', (jid, began)).lastrowid
        try:
            self.check(jid)
            payload = json.loads(job["payload"])
            if job["kind"] == "scan":
                result = self.scan(jid)
            elif job["kind"] == "organize":
                p = self.db.one('SELECT * FROM playlists WHERE id=?', (payload['playlist_id'],))
                if not p:
                    raise ValueError('歌单不存在')
                self.check(jid)
                result = {'queued': len(self.queue_enrichment(self.folder_assets(p['folder'])))}
            elif job["kind"] == "sync":
                result = self.sync(jid, payload["playlist_id"])
            elif job["kind"] in ("acquire", "enrich", "import"):
                result = self.process(jid, job["kind"], payload)
                if job["kind"] in ("acquire", "import"):
                    if not result.get("needs_review"):
                        self.finish_distribution(jid, result)
                        return
            else:
                raise ValueError("未知任务类型")
            self.db.execute(
                "UPDATE jobs SET state=?,stage=?,progress=100,result=?,error=NULL,updated=? WHERE id=?",
                ("review" if result.get("needs_review") else "succeeded", "待确认版本" if result.get("needs_review") else "已完成", dumps(result), now(), jid),
            )
        except Interrupted as e:
            j = self.db.one("SELECT cancel_requested FROM jobs WHERE id=?", (jid,))
            state = "cancelled" if j["cancel_requested"] == 1 else ("paused" if j["cancel_requested"] == 2 else "queued")
            self.db.execute("UPDATE jobs SET state=?,stage=?,updated=? WHERE id=?", (state, str(e), now(), jid))
        except Exception as e:
            text = str(e)[:400]
            if isinstance(e, SourceError):
                text = str(e)
            elif not isinstance(e, ValueError):
                # 不把带签名 URL、Cookie、完整异常栈暴露到产品界面。
                text = f"{type(e).__name__}：任务未完成，请查看来源记录或重试"
            if job['kind'] == 'acquire':
                from .matching import state
                state(self.db, json.loads(job['payload'])['key'], 'failed', text)
            retry = isinstance(e, SourceError) and e.code in ("unavailable", "cooldown", "rate_limited") and job["attempts"] < 2
            self.db.execute(
                "UPDATE jobs SET state=?,stage=?,error=?,next_run=?,updated=? WHERE id=?",
                (
                    "queued" if retry else "failed",
                    "稍后自动重试" if retry else "需要处理",
                    text,
                    now() + 180 * (job["attempts"] + 1),
                    now(),
                    jid,
                ),
            )
        finally:
            ended = now()
            with self.db.connect() as c:
                current = c.execute('SELECT state FROM jobs WHERE id=?', (jid,)).fetchone()['state']
                finished = ended if current in ('succeeded','failed','cancelled','review') else None
                c.execute('UPDATE jobs SET elapsed_seconds=elapsed_seconds+?,run_started_at=NULL,finished_at=? WHERE id=?',
                          (max(0, ended-began), finished, jid))
                c.execute('UPDATE job_runs SET finished=?,outcome=? WHERE id=?', (ended,current,run_id))
            stage = self.config.data / "staging" / jid
            if stage.exists() and not (stage / "intent.json").exists():
                # 未验证的部分下载可重建；失败不累积音频副本。已准备发布的日志必须留作恢复。
                shutil.rmtree(stage)

    def scan(self, jid=None):
        root = self.config.music
        seedfile = self.config.data / "bootstrap.json"
        seed = json.loads(seedfile.read_text()) if seedfile.exists() else {}
        mapping = seed.get("files", {})
        paths = []
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
            dirs[:] = [
                d for d in dirs if d not in ("@eaDir", "#recycle") and not d.startswith(".") and not (Path(directory) / d).is_symlink()
            ]
            paths += [
                Path(directory) / f for f in files if Path(f).suffix.lower() in media.EXTENSIONS and not (Path(directory) / f).is_symlink()
            ]
        seen, errors, pending = [], [], []
        existing_by_path = {a["path"]: a for a in self.db.rows("SELECT * FROM assets")}

        def flush():
            if not pending:
                return
            with self.db.connect() as c:
                for track, values, previous_key in pending:
                    if track:
                        self.db.save_track(track, c)
                    current = c.execute("SELECT data,track_key FROM assets WHERE id=?", (values[0],)).fetchone()
                    if current:
                        data = json.loads(values[3])
                        data["locked"] = json.loads(current["data"]).get("locked", False)
                        values[3] = dumps(data)
                        if current["track_key"] != previous_key:
                            values[2] = current["track_key"]
                    c.execute(
                        """INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                        track_key=excluded.track_key,data=excluded.data,size=excluded.size,mtime_ns=excluded.mtime_ns,
                        sha256=excluded.sha256,present=1,updated=excluded.updated""",
                        values,
                    )
            pending.clear()

        for i, path in enumerate(paths):
            if jid:
                self.check(jid)
            if i % 20 == 0:
                flush()
                if jid:
                    self.update(jid, f"扫描音乐库 {i}/{len(paths)}", round(90 * i / max(1, len(paths))))
            rel = str(path.relative_to(root))
            seen.append(rel)
            existing = existing_by_path.get(rel)
            stat = path.stat()
            sidecar = path.with_suffix(".lrc")
            sidecar_mtime = sidecar.stat().st_mtime_ns if sidecar.is_file() and not sidecar.is_symlink() else 0
            if (
                existing
                and existing["size"] == stat.st_size
                and existing["mtime_ns"] == stat.st_mtime_ns
                and json.loads(existing["data"]).get("sidecar_mtime_ns") == sidecar_mtime
            ):
                continue
            try:
                tags, cover, _ = media.read_tags(path)
                tags["sidecar_mtime_ns"] = sidecar_mtime
                info = mapping.get(unicodedata.normalize("NFC", rel), {})
                track = info.get("track") if info.get("size") == stat.st_size else None
                key = tags["track_key"] or (track["key"] if track else None) or (existing["track_key"] if existing else None)
                if key and not track:
                    track = self.db.track(key)
                if not tags["title"]:
                    tags["title"] = track["title"] if track else path.stem
                if not tags["artists"] and track:
                    tags["artists"] = track.get("artists", [])
                if not tags["album"] and track:
                    tags["album"] = track.get("album", "")
                aid = existing["id"] if existing else uuid.uuid4().hex
                prior = json.loads(existing["data"]) if existing else {}
                tags.update(
                    locked=prior.get("locked", False),
                    match_status=prior.get("match_status"),
                    instrumental=prior.get("instrumental", False),
                    source=prior.get("source") or info.get("source", "本地文件"),
                    lyrics_status=prior.get("lyrics_status") or ("同步歌词" if tags["lyrics_synced"] else "缺少同步歌词"),
                )
                if tags.get("instrumental"):
                    tags["missing"] = [x for x in tags["missing"] if x != "歌词"]
                if cover:
                    media.atomic_bytes(self.config.data / "covers" / (aid + ".jpg"), media.cover_jpeg(cover))
                pending.append(
                    (
                        track,
                        [aid, rel, key, dumps(tags), stat.st_size, stat.st_mtime_ns, None, 1, now()],
                        existing["track_key"] if existing else None,
                    )
                )
            except Exception as e:
                errors.append({"path": rel, "error": type(e).__name__})
        if jid:
            self.check(jid)
        flush()
        # 扫描完整结束才标记失踪；不删除真实文件或数据库历史。
        with self.db.connect() as c:
            c.execute("UPDATE assets SET present=0")
            c.executemany("UPDATE assets SET present=1 WHERE path=?", [(s,) for s in seen])
        self.db.set("last_scan", {"at": now(), "count": len(paths), "errors": errors})
        for folder in sorted({Path(p).parts[0] for p in seen if len(Path(p).parts) > 1}):
            self.local_playlist(folder)
        self.db.execute("UPDATE playlists SET last_check=? WHERE platform='local'", (now(),))
        self.export_all()
        return {"scanned": len(paths), "errors": errors, "written_audio": 0}

    def add_playlist(self, snapshot, mode="watch", auto_add=False, schedule=None, retention="latest"):
        schedule = schedule or ("daily" if snapshot["platform"] == "netease" else "manual")
        validate_options(schedule, retention)
        pid = f"{snapshot['platform']}:{snapshot['source_id']}"
        if self.db.one("SELECT id FROM playlists WHERE id=?", (pid,)):
            raise ValueError("这个歌单已经订阅，请使用立即检查")
        with self.db.connect() as c:
            for track in snapshot["tracks"]:
                self.db.save_track(track, c)
            c.execute(
                """INSERT INTO playlists(id,platform,source_id,name,source_url,cover,enabled,auto_add,snapshot,last_check,next_check,created)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid,
                    snapshot["platform"],
                    snapshot["source_id"],
                    snapshot["name"],
                    snapshot["url"],
                    snapshot.get("cover", ""),
                    int(schedule != "manual"),
                    int(auto_add),
                    dumps(snapshot["tracks"]),
                    now(),
                    next_check(schedule, now()),
                    now(),
                ),
            )
            c.execute("UPDATE playlists SET schedule=?,retention=?,source_kind=?,latest_snapshot=? WHERE id=?",
                      (schedule, retention, snapshot.get('source_kind', 'playlist'), dumps(snapshot['tracks']), pid))
            c.execute(
                "INSERT INTO snapshots(playlist_id,data,diff,created) VALUES (?,?,?,?)",
                (pid, dumps(snapshot["tracks"]), dumps({"initial": True}), now()),
            )
        self.bind_folder(pid)
        queued = []
        if mode == "all":
            queued = self.queue_missing(snapshot["tracks"], pid)
        self.export(pid)
        return {"id": pid, "queued": len(queued)}

    def queue_missing(self, tracks, pid=None):
        result = []
        folder = self.bind_folder(pid) if pid else None
        in_folder = {a["track_key"] for a in self.folder_assets(folder)} if folder else set()
        for t in tracks:
            asset = self.asset_for(t["key"])
            if not asset or (folder and self.canonical(t['key']) not in in_folder):
                result.append(self.queue_track(t, pid))
        return result

    def queue_subscription(self, tracks, pid):
        acquired = {r['track_key'] for r in self.db.rows('SELECT track_key FROM subscription_acquired WHERE playlist_id=?', (pid,))}
        return self.queue_missing([t for t in tracks if t['key'] not in acquired], pid)

    def sync(self, jid, pid):
        p = self.db.one("SELECT * FROM playlists WHERE id=?", (pid,))
        if not p:
            raise ValueError("歌单不存在")
        self.update(jid, "读取完整歌单", 10)
        try:
            fresh = self.sources.playlist(p["platform"], p["source_id"])
            self.check(jid)
            old = json.loads(p['latest_snapshot'] or p['snapshot'])
            diff = snapshot_diff(old, fresh['tracks'])
            members = merge_members(json.loads(p['snapshot']), fresh['tracks'], p['retention'])
            with self.db.connect() as c:
                for t in fresh["tracks"]:
                    self.db.save_track(t, c)
                c.execute(
                    "UPDATE playlists SET name=?,cover=?,snapshot=?,latest_snapshot=?,diff=?,last_check=?,next_check=?,last_error=NULL WHERE id=?",
                    (fresh["name"], fresh.get("cover", ""), dumps(members), dumps(fresh["tracks"]), dumps(diff), now(), next_check(p["schedule"], now()), pid),
                )
                c.execute(
                    "INSERT INTO snapshots(playlist_id,data,diff,created) VALUES (?,?,?,?)",
                    (pid, dumps(fresh["tracks"]), dumps(diff), now()),
                )
                c.execute(
                    "DELETE FROM snapshots WHERE playlist_id=? AND id NOT IN (SELECT id FROM snapshots WHERE playlist_id=? ORDER BY id DESC LIMIT 30)",
                    (pid, pid),
                )
            queued = self.queue_subscription(members, pid) if p['auto_add'] and p['retention'] == 'append' else self.queue_missing(diff['added'], pid) if p['auto_add'] else []
            self.export(pid)
            return {"added": len(diff["added"]), "removed": len(diff["removed"]), "reordered": diff["reordered"], "queued": len(queued)}
        except Exception as e:
            self.db.execute(
                "UPDATE playlists SET last_error=?,next_check=? WHERE id=?",
                (str(e)[:300] if isinstance(e, SourceError) else "检查失败，旧快照已保留", now() + 3600, pid),
            )
            raise

    def canonical(self, key):
        visited = set()
        while key not in visited:
            visited.add(key)
            mapping = self.db.one("SELECT target FROM mappings WHERE original=?", (key,))
            if not mapping:
                return key
            key = mapping["target"]
        raise ValueError("版本映射出现循环，请重新选择")

    def asset_for(self, key):
        key = self.canonical(key)
        for row in self.db.rows("SELECT * FROM assets WHERE track_key=? AND present=1", (key,)):
            p = within(self.config.music, row["path"])
            if p.is_file() and p.stat().st_size == row["size"]:
                return row
        return None

    def process(self, jid, kind, payload):
        stage = self.config.data / "staging" / jid
        stage.mkdir(exist_ok=True)
        intent = stage / "intent.json"
        if intent.exists():
            self.update(jid, "恢复已校验文件的发布", 90)
            return self.publish(jid, json.loads(intent.read_text()), stage)
        asset = self.db.one("SELECT * FROM assets WHERE id=?", (payload.get("asset_id", ""),)) if kind == "enrich" else None
        if asset and json.loads(asset['data']).get('locked'):
            raise ValueError('这首歌的人工修正已锁定，请先解锁')
        track = self.match_asset(asset) if asset else self.db.track(payload.get("key", ""))
        if asset and not track:
            data = json.loads(asset['data'])
            data['match_status'] = 'needs_review'
            self.db.execute('UPDATE assets SET data=? WHERE id=?', (dumps(data), asset['id']))
            return {'asset_id': asset['id'], 'needs_review': True, 'message': '未取得唯一可靠版本，已保留原文件'}
        if not track:
            raise ValueError("尚未匹配到平台歌曲，请先搜索并关联正确版本")
        if kind == 'acquire' and track['platform'] in ('apple', 'spotify'):
            from .matching import resolve
            track = resolve(self, track)
            if track is None:
                return {'needs_review': True, 'message': '未匹配到唯一录音版本，请在歌单中选择版本'}
        if kind in ("acquire", "import"):
            found = self.asset_for(track["key"])
            if found:
                return {"asset_id": found["id"], "reused": True, "message": "库内已有此版本，未重复下载"}
        if asset and json.loads(asset["data"]).get("locked"):
            raise ValueError("这首歌的人工修正已锁定，请先解锁")
        self.update(jid, "核对歌曲版本", 5)
        if track["platform"] == "netease":
            detailed = self.sources.details([track["id"]])
            if len(detailed) != 1:
                raise SourceError("未取得目标歌曲详情", "incomplete")
            track = detailed[0]
            self.db.save_track(track)
        expected = track.get("duration", 0)
        if expected <= 0:
            raise ValueError("目标歌曲缺少可靠时长，不能自动接受下载文件")
        old_hash, relative, aid, old_lyric_hash = None, None, None, None
        source = "本地投递" if kind == "import" else "本地整理"
        if asset:
            original = within(self.config.music, asset["path"])
            if not original.is_file() or original.stat().st_size != asset["size"] or original.stat().st_mtime_ns != asset["mtime_ns"]:
                raise ValueError("文件在扫描后发生变化，请重新扫描再处理")
            old_hash = media.sha(original)
            side = original.with_suffix(".lrc")
            old_lyric_hash = media.sha(side) if side.exists() else None
            raw = stage / ("source" + original.suffix)
            shutil.copyfile(original, raw)
            relative, aid = asset["path"], asset["id"]
            source = json.loads(asset["data"]).get("source", source)
        elif kind == "import":
            original = within(self.config.inbox, payload["file"])
            if not original.is_file() or original.stat().st_size != payload["size"] or original.stat().st_mtime_ns != payload["mtime_ns"]:
                raise ValueError("投递文件已变化，请刷新后重新选择")
            raw = stage / ("source" + original.suffix)
            shutil.copyfile(original, raw)
        else:
            raw = stage / "download.part"
            providers = self.db.get("providers", ["gd", "netease_direct"])
            good = False
            for provider in providers:
                self.check(jid)
                self.update(jid, "获取音频 · " + provider, 15)
                try:
                    candidate = self.sources.audio_url(track, provider)
                    if candidate.get("public_media"):
                        from .lx import download_public
                        download_public(candidate["url"], raw, lambda: self.check(jid))
                    else:
                        self.sources.download(candidate["url"], raw, lambda: self.check(jid))
                    info = media.probe(raw, expected)
                    extension = {"flac": ".flac", "mp3": ".mp3", "aac": ".m4a", "alac": ".m4a", "opus": ".opus", "vorbis": ".ogg"}.get(
                        info["codec_name"]
                    )
                    if not extension:
                        raise ValueError("未接入该音频编码的标签写入")
                    actual = stage / ("source" + extension)
                    raw.replace(actual)
                    raw = actual
                    source, good = provider, True
                    self.attempt(jid, provider, "success", "完整音频与时长验证通过")
                    break
                except Interrupted:
                    raise
                except Exception as e:
                    code = e.code if isinstance(e, SourceError) else "invalid_audio" if isinstance(e, ValueError) else "unavailable"
                    detail = str(e) if isinstance(e, (SourceError, ValueError)) else type(e).__name__
                    self.attempt(jid, provider, code, detail)
            if not good:
                raise ValueError("已尝试全部启用来源，未取得匹配的完整音频；可换源重试或投递本地文件")
        self.check(jid)
        self.update(jid, "校验完整音频", 50)
        info = media.probe(raw, expected)
        before = media.audio_hash(raw)
        tags, _, current_lyric = media.read_tags(raw)
        if asset and not current_lyric:
            side = within(self.config.music, asset["path"]).with_suffix(".lrc")
            if side.is_file():
                current_lyric = side.read_text(encoding="utf-8-sig", errors="replace")
        lyric, cover = None, None
        lyric_state = {"status": "preserved", "label": "保留已有歌词"} if current_lyric else {"status": "missing", "label": "缺少歌词"}
        self.update(jid, "补齐标签、封面与歌词", 65)
        if not current_lyric:
            try:
                original_lyrics = self.sources.lyrics(track)
                media.atomic_bytes(self.config.data / "records" / (jid + "-lyrics.json"), dumps(original_lyrics).encode())
                lyric, lyric_state = media.build_lrc(original_lyrics, info["duration"])
                if "_attempts" in original_lyrics:
                    for attempt in original_lyrics["_attempts"]:
                        name = track["platform"] if attempt["source"] == "platform" else attempt["source"]
                        self.attempt(jid, name + " 歌词", attempt["status"], attempt["detail"])
                else:
                    self.attempt(jid, track["platform"] + " 歌词", lyric_state["status"], lyric_state["label"])
            except SourceError as e:
                lyric_state = {"status": "unavailable", "label": "歌词来源暂不可用"}
                self.attempt(jid, "歌词", e.code, str(e))
        if not tags["has_cover"] and track.get("cover") and payload.get("mode", "all") == "all":
            try:
                cover = media.cover_jpeg(self.sources.request(track["cover"], media=True).content)
            except Exception as e:
                self.attempt(jid, "封面", "unavailable", type(e).__name__)
        # 原有歌词优先保留；仅补缺失项。人工锁定由资产层拦截。
        effective = current_lyric or lyric
        after = media.write_tags(raw, track, lyric=effective, cover=cover, only_missing=True, mode=payload.get("mode", "all"))
        if media.audio_hash(raw) != before:
            raise ValueError("补写后音频数据发生变化，已停止发布")
        self.check(jid)
        aid = aid or uuid.uuid4().hex
        if not relative:
            pid = (payload.get('playlist_ids') or [None])[0]
            _, folder = self.destination(pid)
            relative = self.flat_path(folder, track, raw.suffix)
        target = within(self.config.music, relative)
        if not old_hash and target.exists():
            raise ValueError("目标文件已存在，请扫描后核对，未覆盖")
        lrc_file = stage / "lyrics.lrc"
        if effective:
            lrc_file.write_text(effective, encoding="utf8")
        publication = {
            "id": aid,
            "path": relative,
            "track": track,
            "source": source,
            "audio": raw.name,
            "old_sha256": old_hash,
            "old_lrc_sha256": old_lyric_hash,
            "sha256": media.sha(raw),
            "audio_hash": before,
            "lrc_sha256": media.sha(lrc_file) if effective else None,
            "tags": after,
            "audio_info": info,
            "lyrics": lyric_state,
            "created": now(),
        }
        media.atomic_bytes(intent, dumps(publication).encode())
        self.update(jid, "发布并回读校验", 90)
        return self.publish(jid, publication, stage)

    def publish(self, jid, p, stage):
        target = within(self.config.music, p["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        # durable intent 允许在原子替换后崩溃时恢复，不重复下载或覆盖后来修改。
        current = media.sha(target) if target.exists() else None
        if current not in (p["old_sha256"], p["sha256"]):
            raise ValueError("目标文件发生外部变更，已停止发布")
        if current != p["sha256"]:
            pending = target.with_name("." + target.name + ".musicprep-pending")
            with (stage / p["audio"]).open("rb") as src, pending.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if media.sha(pending) != p["sha256"]:
                raise ValueError("暂存传输校验失败")
            # 再次检查，避免传输期间其他工具修改目标。
            if (media.sha(target) if target.exists() else None) != p["old_sha256"]:
                raise ValueError("发布前目标变化，停止替换")
            os.replace(pending, target)
        if p["lrc_sha256"]:
            side = target.with_suffix(".lrc")
            existing = media.sha(side) if side.exists() else None
            if existing not in (p["old_lrc_sha256"], p["lrc_sha256"]):
                raise ValueError("歌词发生外部变更，已保留，需核对后重试")
            if existing != p["lrc_sha256"]:
                media.atomic_bytes(side, (stage / "lyrics.lrc").read_bytes())
            if media.sha(side) != p["lrc_sha256"]:
                raise ValueError("歌词回读校验失败")
        if media.sha(target) != p["sha256"]:
            raise ValueError("音频回读校验失败")
        tags, cover, _ = media.read_tags(target)
        tags.update(
            source=p["source"],
            locked=False,
            lyrics_status=p["lyrics"]["label"],
            instrumental=p["lyrics"]["status"] == "instrumental",
            audio_info=p["audio_info"],
            sidecar_mtime_ns=target.with_suffix(".lrc").stat().st_mtime_ns if target.with_suffix(".lrc").is_file() else 0,
        )
        if tags.get("instrumental"):
            tags["missing"] = [x for x in tags["missing"] if x != "歌词"]
        if cover:
            media.atomic_bytes(self.config.data / "covers" / (p["id"] + ".jpg"), media.cover_jpeg(cover))
        st = target.stat()
        self.db.execute(
            """INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                          track_key=excluded.track_key,data=excluded.data,size=excluded.size,mtime_ns=excluded.mtime_ns,
                          sha256=excluded.sha256,present=1,updated=excluded.updated""",
            (p["id"], p["path"], p["track"]["key"], dumps(tags), st.st_size, st.st_mtime_ns, p["sha256"], 1, now()),
        )
        media.atomic_bytes(self.config.data / "records" / (jid + ".json"), dumps({**p, "verified_at": now()}).encode())
        self.export_all()
        # 成功后保留小型溯源记录，清除自己生成的音频暂存；不备份整库。
        shutil.rmtree(stage)
        return {"asset_id": p["id"], "path": p["path"], "sha256": p["sha256"], "lyrics": p["lyrics"], "source": p["source"]}

    def export(self, pid):
        p = self.db.one("SELECT * FROM playlists WHERE id=?", (pid,))
        if not p:
            return None
        rows = ["#EXTM3U"]
        missing = 0
        for t in self.playlist_tracks(p):
            if t['available']:
                label = ("、".join(t["artists"]) + " - " + t["title"]).replace("\n", " ").replace("\r", " ")
                rows.extend([f"#EXTINF:{round(t.get('duration', 0))},{label}", t['path']])
            else:
                missing += 1
        name = "留声 - " + media.safe_name(p["name"]) + " [" + media.safe_name(pid) + "].m3u8"
        destination = self.config.music
        if p['folder']:
            destination = within(self.config.music, p['folder'])
            destination.mkdir(exist_ok=True)
            rows = [os.path.relpath(self.config.music / r, destination) if r and not r.startswith('#') else r for r in rows]
            name = '歌单.m3u8'
        media.atomic_bytes(destination / name, ("\n".join(rows) + "\n").encode())
        return {"file": str((destination / name).relative_to(self.config.music)), "available": (len(rows) - 1) // 2, "missing": missing}

    def export_all(self):
        for p in self.db.rows("SELECT id FROM playlists"):
            self.export(p["id"])
