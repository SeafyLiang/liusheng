import hmac
import json
import sqlite3
import os
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path

import psutil
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, media
from .accounts import Accounts, member_allowed, password_hash
from .personal import Personal
from .downloads import Downloads
from .mobile import install_api
from .task_control import policy, rate_status, timing, validate_policy
from .config import Config, within
from .db import Database, dumps, now
from .engine import Engine
from .playback import MIME, Playback
from .sources import SourceError, Sources
from .lx import LIMIT as LX_LIMIT, LXManager
from .subscriptions import next_check, validate_options, counts
from .charts import CATALOG


class Login(BaseModel):
    username: str = Field(default="admin", min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=256)


class Preview(BaseModel):
    text: str = Field(min_length=4, max_length=2000)


class Subscribe(BaseModel):
    preview_id: str
    mode: str = "watch"
    auto_add: bool = False
    schedule: str | None = None
    retention: str = "latest"


class Enrich(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=100)
    mode: str = "all"


def create_app(config=None):
    cfg = config or Config.env()
    cfg.prepare()
    db = Database(cfg.data / "music.sqlite3")
    sources = Sources(db)
    lx = LXManager(cfg, db)
    sources.lx = lx
    engine = Engine(cfg, db, sources)
    playback = Playback(cfg, db, sources)
    if not db.get("auth"):
        salt = secrets.token_hex(16)
        password = secrets.token_urlsafe(18)
        db.set("auth", {"salt": salt, "hash": password_hash(password, salt)})
        p = cfg.data / "initial-password.txt"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password + "\n")
    started = now()
    accounts = Accounts(db)
    personal = Personal(db)
    downloads = Downloads(cfg, db, personal)
    cover_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        personal.start()
        if cfg.worker:
            engine.start()
        yield
        downloads.close()
        playback.close()
        engine.close()
        personal.close()
        sources.close()

    app = FastAPI(title="留声 · 个人音乐服务", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db, app.state.engine, app.state.sources, app.state.config = db, engine, sources, cfg
    app.state.playback = playback
    app.state.lx = lx

    app.state.accounts, app.state.personal, app.state.downloads = accounts, personal, downloads
    session = accounts.session
    install_api(app, db, cfg, accounts, personal, downloads, engine)

    @app.middleware("http")
    async def security(request: Request, call_next):
        path = request.url.path
        public = path in ("/api/health", "/api/session", "/api/login", "/api/v1/auth/login", "/api/v1/releases/android")
        if path.startswith("/api/") and not public:
            s = session(request)
            if not s:
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            request.state.session = s
            if s['role'] != 'admin' and not member_allowed(path, request.method):
                return JSONResponse({"detail": "仅管理员可以执行此操作"}, status_code=403)
            if s['kind'] == 'web' and request.method not in ("GET", "HEAD", "OPTIONS") and not hmac.compare_digest(
                s["csrf"], request.headers.get("x-csrf-token", "")
            ):
                return JSONResponse({"detail": "页面会话已更新，请刷新后重试"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            allowed = set(db.get("allowed_origins", [])) | {str(request.base_url).rstrip("/")}
            if origin and origin not in allowed:
                return JSONResponse({"detail": "不接受跨站提交"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不接受跨站提交"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        if 'Cache-Control' not in response.headers and (path.startswith("/api/") or path in ("/", "/sw.js", "/manifest.webmanifest")):
            response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
        return response

    @app.exception_handler(SourceError)
    async def source_error(_, exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=502)

    @app.exception_handler(ValueError)
    async def value_error(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    def health():
        worker = engine.health_status() if cfg.worker else {"worker": True, "worker_state": "disabled"}
        database_ok, last_finished = True, None
        try:
            address = db.path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(address, uri=True, timeout=0.25)) as connection:
                last_finished = connection.execute("SELECT max(finished_at) FROM jobs").fetchone()[0]
        except (sqlite3.Error, OSError):
            database_ok = False
        healthy = database_ok and worker["worker"] and worker["worker_state"] in ("running", "disabled")
        return JSONResponse({
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            **worker,
            "database": "ok" if database_ok else "unavailable",
            "last_job_finished_at": last_finished,
        }, status_code=200 if healthy else 503)

    @app.get("/api/session")
    def current_session(request: Request):
        s = session(request)
        return {"authenticated": bool(s), "csrf": s["csrf"] if s else None, "version": __version__,
                "user": {"id": s["user_id"], "username": s["username"], "role": s["role"]} if s else None,
                "server_id": db.get('server_id')}

    @app.post("/api/login")
    def login(body: Login, request: Request):
        result = accounts.login(body.username, body.password, '浏览器', request)
        token = result.pop('token')
        response = JSONResponse(result)
        response.set_cookie(
            "music_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400
        )
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        s = session(request)
        db.execute("DELETE FROM sessions WHERE token=?", (s["token"],))
        r = JSONResponse({"ok": True})
        r.delete_cookie("music_session")
        return r

    @app.post("/api/password")
    def change_password(body: dict, request: Request):
        accounts.change_password(request.state.session['user_id'], str(body.get('current', '')), str(body.get('password', '')))
        if request.state.session['username'] == 'admin':
            (cfg.data / 'initial-password.txt').unlink(missing_ok=True)
        return {"ok": True}

    def asset_json(a):
        return {
            **json.loads(a["data"]),
            "id": a["id"],
            "path": a["path"],
            "track_key": a["track_key"],
            "size": a["size"],
            "present": bool(a["present"]),
            "updated": a["updated"],
            "sha256": a["sha256"],
        }

    def job_json(j):
        return {k: v for k, v in j.items() if k not in ("payload", "unique_key")} | {
            "result": json.loads(j["result"]) if j["result"] else None,
            **timing(j, now()),
        }

    @app.get("/api/overview")
    def overview():
        assets = [asset_json(a) for a in db.rows("SELECT * FROM assets WHERE present=1")]
        states = {r["state"]: r["n"] for r in db.rows("SELECT state,count(*) n FROM jobs GROUP BY state")}
        return {
            "assets": len(assets),
            "bytes": sum(a["size"] for a in assets),
            "missing_lyrics": sum(not a.get("has_lyrics") and not a.get("instrumental") for a in assets),
            "missing_cover": sum(not a.get("has_cover") for a in assets),
            "needs_tags": sum(bool(a.get("missing")) for a in assets),
            "playlists": db.one("SELECT count(*) n FROM playlists")["n"],
            "jobs": states,
            "queue_paused": db.get("queue_paused", False),
            "recent": assets[-6:][::-1],
            "last_scan": db.get("last_scan"),
            "active": [
                job_json(j)
                for j in db.rows(
                    "SELECT * FROM jobs WHERE state IN ('running','queued','failed') ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,updated DESC LIMIT 5"
                )
            ],
        }

    @app.get("/api/library")
    def library(q: str = "", filter: str = "all", page: int = 1, folder: str = ""):
        if len(q) > 200:
            raise ValueError("检索内容过长")
        where, args = ['present=?'], [0 if filter == 'missing' else 1]
        if q:
            where.append("instr(casefold(json_extract(data,'$.title') || ' ' || json_extract(data,'$.artists') || ' ' || json_extract(data,'$.album')),?)>0")
            args.append(q.casefold())
        if folder:
            where.append('substr(path,1,?)=?')
            args.extend([len(folder) + 1, folder + '/'])
        if filter in ('lyrics', 'cover'):
            where.append("coalesce(json_extract(data,'$.has_" + filter + "'),0)=0")
        if filter == 'lyrics':
            where.append("coalesce(json_extract(data,'$.instrumental'),0)=0")
        elif filter == 'incomplete':
            where.append("json_array_length(json_extract(data,'$.missing'))>0")
        elif filter == 'review':
            where.append("json_extract(data,'$.match_status')='needs_review'")
        condition = ' AND '.join(where)
        total = db.one('SELECT count(*) n FROM assets WHERE ' + condition, args)['n']
        page = max(1, page)
        rows = db.rows('SELECT * FROM assets WHERE ' + condition + ' ORDER BY path COLLATE NOCASE LIMIT 40 OFFSET ?', args + [(page-1)*40])
        return {'items': [asset_json(a) for a in rows], 'total': total, 'page': page}

    @app.get("/api/library/issues")
    def library_issues():
        return {'items': (db.get('last_scan') or {}).get('errors', [])}

    @app.post("/api/library/scan")
    def scan():
        return {"job_id": engine.enqueue("scan", {}, "扫描音乐库")}

    @app.post("/api/library/enrich")
    def enrich(body: Enrich):
        if body.mode not in ("all", "lyrics"):
            raise ValueError("未知整理方式")
        ids = []
        for aid in set(body.asset_ids):
            a = db.one("SELECT * FROM assets WHERE id=? AND present=1", (aid,))
            if not a:
                raise ValueError("歌曲已不存在，请重新扫描")
            if json.loads(a["data"]).get("locked"):
                raise ValueError("选择中包含已锁定的人工修正，请先取消选择或解锁")
        for aid in set(body.asset_ids):
            a = db.one("SELECT data FROM assets WHERE id=?", (aid,))
            ids.append(engine.enqueue("enrich", {"asset_id": aid, "mode": body.mode}, "补齐 · " + json.loads(a["data"])["title"]))
        return {"jobs": ids}

    @app.patch("/api/library/{asset_id}")
    def patch_asset(asset_id: str, body: dict):
        a = db.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if not a:
            raise HTTPException(404, "歌曲不存在")
        data = json.loads(a["data"])
        if "locked" in body:
            data["locked"] = bool(body["locked"])
        key = a["track_key"]
        if "track_key" in body:
            t = db.track(str(body["track_key"]))
            if not t:
                raise ValueError("请选择有效的搜索结果")
            if abs(t.get("duration", 0) - data.get("duration", 0)) > max(3, data.get("duration", 0) * 0.02):
                raise ValueError("歌曲时长不匹配，不能关联到此版本")
            key = t["key"]
        db.execute("UPDATE assets SET data=?,track_key=?,updated=? WHERE id=?", (dumps(data), key, now(), asset_id))
        return {"ok": True}

    @app.get("/api/library/{asset_id}/detail")
    def asset_detail(asset_id: str):
        a = db.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if not a:
            raise HTTPException(404)
        path = within(cfg.music, a["path"])
        lyric = media.cached_lyrics(path) if path.exists() else ""
        return {**asset_json(a), "lyrics_text": lyric, "track": db.track(a["track_key"]) if a["track_key"] else None}

    @app.get("/api/covers/{asset_id}")
    def cover(asset_id: str, request: Request):
        if not asset_id.isalnum():
            raise HTTPException(404)
        path = cfg.data / "covers" / (asset_id + ".jpg")
        if not path.is_file():
            with cover_lock:
                if not path.is_file():
                    a = db.one('SELECT path,data FROM assets WHERE id=? AND present=1', (asset_id,))
                    if not a:
                        raise HTTPException(404)
                    source = within(cfg.music, a['path'])
                    picture = media.read_tags(source)[1] if source.is_file() else None
                    if not picture:
                        raise HTTPException(404)
                    media.atomic_bytes(path, media.cover_jpeg(picture))
        st = path.stat()
        etag = f'"{asset_id}-{st.st_mtime_ns}-{st.st_size}"'
        headers = {'ETag': etag, 'Cache-Control': 'private, no-cache'}
        if request.headers.get('if-none-match') == etag:
            return Response(status_code=304, headers=headers)
        return FileResponse(path, media_type="image/jpeg", headers=headers)

    @app.api_route("/api/library/{asset_id}/audio", methods=["GET", "HEAD"])
    def asset_audio(asset_id: str):
        _, path = playback.asset(asset_id)
        return FileResponse(path, media_type=MIME[path.suffix.lower()], content_disposition_type="inline")

    @app.post("/api/playback/prepare")
    def prepare_audio(body: dict):
        if body.get("asset_id"):
            return playback.prepare(asset_id=str(body["asset_id"]))
        key = engine.canonical(str(body.get("key", "")))
        track = db.track(key)
        if not track:
            raise HTTPException(404, "请先搜索并选择歌曲")
        a = engine.asset_for(key)
        if a:
            playback.asset(a["id"])
            return {"state": "ready", "url": f"/api/library/{a['id']}/audio", "asset_id": a["id"], "label": "音乐库"}
        return playback.prepare(track=track)

    @app.get("/api/playback/preparations/{ident}")
    def preparation(ident: str):
        return playback.public(ident)

    @app.api_route("/api/playback/cache/{ident}", methods=["GET", "HEAD"])
    def cached_audio(ident: str):
        return FileResponse(playback.cached(ident), media_type="audio/mpeg", content_disposition_type="inline")

    @app.get("/api/playback/lyrics")
    def playback_lyrics(key: str):
        track = db.track(engine.canonical(key))
        if not track:
            raise HTTPException(404, "歌曲不存在")
        raw = sources.lyrics(track)
        lyric, _ = media.build_lrc(raw, track.get("duration") or 3600)
        return {"lyrics_text": lyric or raw.get("lrc", {}).get("lyric", "")}

    @app.get("/api/search")
    def search(q: str, platform: str = "netease", page: int = 1):
        if not 1 <= len(q.strip()) <= 160 or page < 1 or page > 20:
            raise ValueError("请输入 1 至 160 字的歌名或歌手")
        tracks = sources.search(q.strip(), platform, page)
        with db.connect() as connection:
            for t in tracks:
                db.save_track(t, connection)
        for t in tracks:
            a = engine.asset_for(t["key"])
            t["asset_id"] = a["id"] if a else None
        return {"items": tracks, "page": page, "platform": platform}

    @app.post("/api/acquire")
    def acquire(body: dict):
        key = engine.canonical(str(body.get("key", "")))
        t = db.track(key)
        if not t:
            raise ValueError("请先搜索并选择版本")
        pid, folder = engine.destination(body.get('playlist_id'))
        db.set('default_playlist', pid)
        existing = next((a for a in engine.folder_assets(folder) if a['track_key'] == key), None)
        if existing:
            return {'existing': True, 'asset_id': existing['id']}
        return {"job_id": engine.queue_track(t, pid)}

    @app.post("/api/mappings")
    def mapping(body: dict):
        original, target = str(body.get("original", "")), str(body.get("target", ""))
        a, b = db.track(original), db.track(target)
        if not a or not b or original == target:
            raise ValueError("请选择不同的有效候选")
        target = engine.canonical(target)
        if target == original:
            raise ValueError("不能建立循环映射")
        if not a.get("duration") or not b.get("duration") or abs(a["duration"] - b["duration"]) > max(3, a["duration"] * 0.02):
            raise ValueError("候选时长不符，请核对录音版本")
        if body.get("confirmed") is not True:
            raise ValueError("请确认这两个条目是相同录音版本")
        db.execute(
            "INSERT INTO mappings VALUES (?,?,?) ON CONFLICT(original) DO UPDATE SET target=excluded.target,created=excluded.created",
            (original, target, now()),
        )
        engine.export_all()
        return {"ok": True}

    @app.get('/api/chart-sources')
    def chart_sources():
        statuses = db.get('chart_source_status', {})
        return {'items': [{**item, 'playlist_id': item['platform'] + ':' + item['source_id'],
                           'subscribed': bool(db.one('SELECT id FROM playlists WHERE id=?', (item['platform'] + ':' + item['source_id'],))),
                           'last_error': statuses.get(item['platform'], '')} for item in CATALOG]}

    @app.post("/api/playlists/preview")
    def preview(body: Preview):
        platform, ident = sources.resolve(body.text)
        data = sources.playlist(platform, ident)
        preview_id = uuid.uuid4().hex
        db.execute("INSERT INTO previews VALUES (?,?,?)", (preview_id, dumps(data), now() + 1800))
        existing = sum(bool(engine.asset_for(t["key"])) for t in data["tracks"])
        return {**data, "tracks": data["tracks"][:8], "preview_id": preview_id, "existing": existing}

    @app.post("/api/playlists")
    def subscribe(body: Subscribe):
        p = db.one("SELECT data FROM previews WHERE id=? AND expires>?", (body.preview_id, now()))
        if not p or body.mode not in ("all", "watch"):
            raise ValueError("预览已过期，请重新读取歌单")
        return engine.add_playlist(json.loads(p["data"]), body.mode, body.auto_add, body.schedule, body.retention)

    def playlist_json(p, details=False):
        tracks = engine.playlist_tracks(p)
        return {k: v for k, v in p.items() if k not in ('snapshot', 'latest_snapshot')} | {
            **counts(db, p), 'diff': json.loads(p['diff']), 'count': len(tracks),
            'available': sum(t['available'] for t in tracks), 'tracks': tracks if details else []}

    @app.get("/api/playlists")
    def playlists():
        assets = db.rows('SELECT path,track_key FROM assets WHERE present=1')
        keys = {a['track_key'] for a in assets}
        folders = {}
        for a in assets:
            folder = a['path'].split('/')[0]
            folders[folder] = folders.get(folder, 0) + 1
        mappings = {r['original']: r['target'] for r in db.rows('SELECT * FROM mappings')}
        items = []
        for p in db.rows('SELECT * FROM playlists ORDER BY created DESC'):
            if p['platform'] == 'local':
                count = available = folders.get(p['folder'], 0)
            else:
                tracks = json.loads(p['snapshot'])
                count, available = len(tracks), 0
                for t in tracks:
                    key, seen = t['key'], set()
                    while key in mappings and key not in seen:
                        seen.add(key)
                        key = mappings[key]
                    available += key in keys
            items.append({k: v for k, v in p.items() if k not in ('snapshot', 'latest_snapshot')} | {
                **counts(db, p), 'diff': json.loads(p['diff']), 'count': count, 'available': available, 'tracks': []})
        return {'items': items}

    @app.get("/api/playlists/{playlist_id}")
    def playlist(playlist_id: str):
        p = db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
        if not p:
            raise HTTPException(404)
        return playlist_json(p, True)

    @app.patch("/api/playlists/{playlist_id}")
    def update_playlist(playlist_id: str, body: dict):
        p = db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
        if not p:
            raise HTTPException(404)
        if p['platform'] == 'local':
            raise ValueError('本地歌单不设置远程同步规则')
        schedule = body.get('schedule', p['schedule'])
        retention = body.get('retention', p['retention'])
        validate_options(schedule, retention)
        enabled = bool(body.get('enabled', p['enabled']))
        if 'schedule' in body and schedule != p['schedule']:
            enabled = schedule != 'manual'
        if enabled and schedule == 'manual':
            raise ValueError('请先选择每日或每周更新规则')
        due = next_check(schedule, now()) if schedule != p['schedule'] or (enabled and not p['enabled']) else p['next_check']
        db.execute(
            'UPDATE playlists SET enabled=?,auto_add=?,schedule=?,retention=?,next_check=? WHERE id=?',
            (int(enabled), int(bool(body.get('auto_add', p['auto_add']))), schedule, retention, due, playlist_id))
        return {"ok": True}

    @app.post("/api/playlists/{playlist_id}/refresh")
    def refresh(playlist_id: str):
        p = db.one("SELECT name FROM playlists WHERE id=?", (playlist_id,))
        if not p:
            raise HTTPException(404)
        full = db.one('SELECT platform FROM playlists WHERE id=?', (playlist_id,))
        if full['platform'] == 'local':
            return {'job_id': engine.enqueue('scan', {}, '扫描本地歌单')}
        return {"job_id": engine.enqueue("sync", {"playlist_id": playlist_id}, "检查歌单 · " + p["name"])}

    @app.post("/api/playlists/{playlist_id}/missing")
    def missing(playlist_id: str):
        p = db.one("SELECT snapshot FROM playlists WHERE id=?", (playlist_id,))
        if not p:
            raise HTTPException(404)
        return {"jobs": engine.queue_missing(json.loads(p["snapshot"]), playlist_id)}

    @app.post("/api/playlists/{playlist_id}/enrich")
    def organize_playlist(playlist_id: str):
        p = db.one('SELECT name FROM playlists WHERE id=?', (playlist_id,))
        if not p:
            raise HTTPException(404, '歌单不存在')
        return {'job_id': engine.enqueue('organize', {'playlist_id': playlist_id}, '整理歌单 · ' + p['name'])}

    @app.get("/api/playlists/{playlist_id}/export")
    def export(playlist_id: str):
        result = engine.export(playlist_id)
        if not result:
            raise HTTPException(404)
        return FileResponse(cfg.music / result["file"], filename=result["file"], media_type="audio/x-mpegurl")

    @app.get("/api/jobs")
    def jobs(state: str = "all", page: int = 1):
        where, args = ('', []) if state == 'all' else (' WHERE state=?', [state])
        count = db.one('SELECT count(*) n FROM jobs' + where, args)['n']
        page = max(1, page)
        rows = db.rows('SELECT * FROM jobs' + where + ' ORDER BY created DESC LIMIT 40 OFFSET ?', args + [(page - 1) * 40])
        states = {r['state']:r['n'] for r in db.rows('SELECT state,count(*) n FROM jobs GROUP BY state')}
        summary = {'total': sum(states.values()), 'states': states,
                   'finished_last_day': db.one("SELECT count(*) n FROM jobs WHERE finished_at>=? AND state='succeeded'", (now()-86400,))['n'],
                   'average_seconds': db.one("SELECT avg(elapsed_seconds) n FROM jobs WHERE state='succeeded' AND timing_partial=0 AND started_at IS NOT NULL")['n']}
        return {'items': [job_json(j) for j in rows], 'total': count, 'summary': summary,
                'rate_limit': rate_status(db,now()), 'queue_paused': db.get('queue_paused', False)}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        j = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise HTTPException(404)
        return {**job_json(j), "attempts_log": db.rows("SELECT * FROM attempts WHERE job_id=? ORDER BY id", (job_id,))}

    @app.post("/api/jobs/{job_id}/{action}")
    def job_action(job_id: str, action: str):
        j = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise HTTPException(404)
        if action in ("retry", "resume") and j["state"] in ("failed", "paused", "cancelled", "review"):
            if db.one(
                "SELECT id FROM jobs WHERE unique_key=? AND state IN ('queued','running','paused') AND id!=?", (j["unique_key"], job_id)
            ):
                raise ValueError("已有同一歌曲的任务正在处理")
            db.execute(
                "UPDATE jobs SET state='queued',error=NULL,cancel_requested=0,next_run=0,attempts=0,finished_at=NULL,stage='等待处理',updated=? WHERE id=?",
                (now(), job_id),
            )
        elif action in ("pause", "cancel") and j["state"] in ("queued", "running", "paused"):
            state = j["state"] if j["state"] == "running" else ("paused" if action == "pause" else "cancelled")
            db.execute(
                "UPDATE jobs SET state=?,cancel_requested=?,updated=? WHERE id=?", (state, 2 if action == "pause" else 1, now(), job_id)
            )
            if state == 'cancelled':
                db.execute('UPDATE jobs SET finished_at=? WHERE id=?', (now(),job_id))
        else:
            raise ValueError("这个任务当前不能执行该操作")
        return {"ok": True}

    @app.get("/api/inbox")
    def inbox():
        items = []
        for p in cfg.inbox.rglob("*"):
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in media.EXTENSIONS and p.resolve().is_relative_to(cfg.inbox):
                s = p.stat()
                items.append(
                    {
                        "file": str(p.relative_to(cfg.inbox)),
                        "size": s.st_size,
                        "mtime_ns": s.st_mtime_ns,
                        "stable": time.time() - s.st_mtime > 5,
                    }
                )
        return {"items": items[:500]}

    @app.post("/api/inbox/upload")
    async def upload(file: UploadFile = File(...)):
        name = media.safe_name(Path(file.filename or "音频").stem)
        ext = Path(file.filename or "").suffix.lower()
        if ext not in media.EXTENSIONS:
            raise ValueError("请选择音频文件")
        target = cfg.inbox / (name + "-" + uuid.uuid4().hex[:8] + ext)
        part = target.with_suffix(ext + ".upload")
        size = 0
        try:
            with part.open("xb") as f:
                while data := await file.read(1024 * 1024):
                    size += len(data)
                    if size > 512 * 1024 * 1024:
                        raise ValueError("单个文件不能超过 512 MiB")
                    f.write(data)
            part.rename(target)
        finally:
            part.unlink(missing_ok=True)
            await file.close()
        return {"file": target.name, "size": size}

    @app.post("/api/inbox/import")
    def import_file(body: dict):
        file, key = str(body.get("file", "")), str(body.get("key", ""))
        path = within(cfg.inbox, file)
        t = db.track(key)
        if not path.is_file() or path.suffix.lower() not in media.EXTENSIONS or not t:
            raise ValueError("请选择投递文件和对应搜索结果")
        st = path.stat()
        if time.time() - st.st_mtime < 5:
            raise ValueError("文件刚刚写入，请稍后再导入")
        pid, folder = engine.destination(body.get('playlist_id'))
        db.set('default_playlist', pid)
        if any(a['track_key'] == engine.canonical(key) for a in engine.folder_assets(folder)):
            return {'existing': True}
        payload = {"key": key, "file": file, "size": st.st_size, "mtime_ns": st.st_mtime_ns, 'playlist_ids': [pid]}
        return {"job_id": engine.enqueue("import", payload, "投递 · " + t["title"], unique_key="acquire:" + key)}

    @app.get("/api/settings")
    def settings():
        return {
            "task_policy": policy(db),
            "providers": db.get("providers", ["gd", "netease_direct"]),
            "provider_names": {"gd": "GD 音乐台", "netease_direct": "网易云公开音频", "tencent_direct": "QQ 公开音频（独立渠道）",
                               **{"lx:" + s["id"]: s["name"] for s in lx.items()}},
            "available_providers": ["gd", "netease_direct", "tencent_direct", *lx.eligible()],
            "lyric_providers": db.get("lyric_providers", ["platform", "lrclib"]),
            "lx_sources": lx.items(),
            "lx_runner_configured": bool(os.getenv("LX_RUNNER_URL") and os.getenv("LX_RUNNER_TOKEN")),
            "default_playlist": db.get("default_playlist"),
            "music_root": str(cfg.music),
            "queue_paused": db.get("queue_paused", False),
            "source_health": db.rows("SELECT * FROM source_health"),
            "music_path": str(cfg.music),
            "inbox_path": str(cfg.inbox),
            "platforms": [
                {"id": "netease", "name": "网易云", "detail": "公开歌单每日检查；搜索与歌词"},
                {"id": "tencent", "name": "QQ 音乐", "detail": "完整公开歌单导入；手动检查；搜索受接口状态影响"},
                {"id": "kuwo", "name": "酷我音乐", "detail": "歌曲检索与 LX 平台匹配；未接入歌单同步或独立音频渠道"},
                {"id": "qishui", "name": "汽水音乐", "detail": "手动输入歌名查找对应版本"},
            ],
            "manual_sources": [],
        }

    @app.patch("/api/settings")
    def save_settings(body: dict):
        if 'task_policy' in body:
            db.set('task_policy', validate_policy(body['task_policy']))
        if "providers" in body:
            p = body["providers"]
            available = ["gd", "netease_direct", "tencent_direct", *lx.eligible()]
            if not isinstance(p, list) or any(not isinstance(x, str) for x in p) or len(p) != len(set(p)) or any(x not in available for x in p):
                raise ValueError("请选择已接入且通过验证的来源；限制批量的 LX 源不能加入任务")
            db.set("providers", p)
        if "lyric_providers" in body:
            p = body["lyric_providers"]
            if not isinstance(p, list) or any(not isinstance(x, str) or x not in ("platform", "lrclib") for x in p) or len(p) != len(set(p)):
                raise ValueError("歌词来源配置无效")
            db.set("lyric_providers", p)
        if "queue_paused" in body:
            db.set("queue_paused", bool(body["queue_paused"]))
        return {"ok": True}

    @app.post("/api/lx/import-url")
    def lx_import_url(body: dict):
        url = body.get("url")
        if not isinstance(url, str) or len(url) > 3000:
            raise ValueError("请提供脚本链接")
        return lx.import_url(url)

    @app.post("/api/lx/import-file")
    async def lx_import_file(file: UploadFile = File(...)):
        try:
            if not (file.filename or "").lower().endswith(".js"):
                raise ValueError("请选择 .js 音源文件")
            return lx.import_script(await file.read(LX_LIMIT + 1))
        finally:
            await file.close()

    @app.post("/api/lx/{ident}/inspect")
    def lx_inspect(ident: str):
        return lx.inspect(ident)

    @app.post("/api/lx/{ident}/probe")
    def lx_probe(ident: str, body: dict):
        track = db.track(str(body.get("key", "")))
        if not track:
            raise ValueError("请先搜索并选择用于验证的歌曲")
        return lx.probe(ident, track)

    @app.get("/api/system")
    def system():
        process = psutil.Process()
        children = process.children(recursive=True)
        memory = process.memory_info().rss + sum(c.memory_info().rss for c in children if c.is_running())
        cpu_seconds = sum(process.cpu_times()[:2])
        disk = os.statvfs(cfg.music)
        limit = None
        usage = None
        for f in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
            try:
                n = Path(f).read_text().strip()
                if n.isdigit() and int(n) < 2**60:
                    limit = int(n)
                    break
            except OSError:
                pass
        for f in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            try:
                usage = int(Path(f).read_text().strip())
                break
            except (OSError, ValueError):
                pass
        return {
            "version": __version__,
            "uptime_seconds": now() - started,
            "process_memory_bytes": memory,
            "container_memory_bytes": usage,
            "memory_limit_bytes": limit,
            "cpu_seconds": cpu_seconds,
            "disk_free_bytes": disk.f_bavail * disk.f_frsize,
            "worker_alive": bool(engine.thread and engine.thread.is_alive()),
            "worker_heartbeat": engine.heartbeat,
            "queue_paused": db.get("queue_paused", False),
            "database_bytes": (cfg.data / "music.sqlite3").stat().st_size,
            "audio_concurrency": 1,
        }

    if cfg.static.exists():
        app.mount("/", StaticFiles(directory=cfg.static, html=True), name="web")
    return app
