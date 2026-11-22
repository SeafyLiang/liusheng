"""API v1: native clients and the shared personal web interface."""
import json
from collections import OrderedDict
import hashlib
import threading
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import media
from .accounts import public_user
from .config import within


def install_api(app, db, cfg, accounts, personal, downloads, engine):
    router = APIRouter(prefix='/api/v1')
    public_cache = OrderedDict()
    public_lock = threading.Lock()

    def uid(request):
        return request.state.session['user_id']

    def admin(request):
        if request.state.session['role'] != 'admin':
            raise HTTPException(403, '仅管理员可以执行此操作')

    @router.post('/auth/login')
    def login(body: dict, request: Request):
        username, password = str(body.get('username', '')), str(body.get('password', ''))
        if len(username) > 40 or not 1 <= len(password) <= 256:
            raise ValueError('用户名或密码格式无效')
        result = accounts.login(username, password, str(body.get('device_name', 'Android')), request, 'device')
        result.pop('csrf')
        return result

    @router.get('/me')
    def me(request: Request):
        user = db.one('SELECT * FROM users WHERE id=?', (uid(request),))
        return {'user': public_user(user), 'server_id': db.get('server_id'), 'api_version': 1,
                'capabilities': ['personal', 'offline', 'sync', 'android_updates']}

    @router.post('/auth/logout')
    def logout(request: Request):
        db.execute('DELETE FROM sessions WHERE token=?', (request.state.session['token'],))
        return {'ok': True}

    @router.get('/auth/sessions')
    def sessions(request: Request):
        return {'items': db.rows('SELECT id,kind,device_name,expires FROM sessions WHERE user_id=?', (uid(request),))}

    @router.delete('/auth/sessions/{session_id}')
    def revoke(session_id: str, request: Request):
        db.execute('DELETE FROM sessions WHERE id=? AND user_id=?', (session_id, uid(request)))
        return {'ok': True}

    @router.post('/auth/password')
    def password(body: dict, request: Request):
        accounts.change_password(uid(request), str(body.get('current', '')), str(body.get('password', '')))
        if request.state.session['role'] == 'admin':
            (cfg.data / 'initial-password.txt').unlink(missing_ok=True)
        return {'ok': True}

    @router.get('/admin/users')
    def users(request: Request):
        admin(request)
        return {'items': [public_user(u) for u in db.rows('SELECT * FROM users ORDER BY created,id')]}

    @router.post('/admin/users')
    def create_user(body: dict, request: Request):
        admin(request)
        return accounts.create(str(body.get('username', '')), str(body.get('password', '')))

    @router.patch('/admin/users/{user_id}')
    def update_user(user_id: str, body: dict, request: Request):
        admin(request)
        user = db.one('SELECT * FROM users WHERE id=?', (user_id,))
        if not user:
            raise HTTPException(404, '账号不存在')
        if 'enabled' in body:
            if type(body['enabled']) is not bool:
                raise ValueError('账号状态无效')
            if user['role'] == 'admin' and not body['enabled']:
                raise ValueError('不能停用管理员账号')
            with db.connect() as c:
                c.execute('UPDATE users SET enabled=? WHERE id=?', (int(body['enabled']), user_id))
                if not body['enabled']:
                    c.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
        if 'password' in body:
            accounts.set_password(user_id, str(body['password']))
            if user['role'] == 'admin':
                (cfg.data / 'initial-password.txt').unlink(missing_ok=True)
        return public_user(db.one('SELECT * FROM users WHERE id=?', (user_id,)))

    @router.get('/library')
    def library(q: str = '', page: int = 1, limit: int = 100, folder: str = ''):
        if len(q) > 200 or not 1 <= limit <= 500 or page < 1:
            raise ValueError('查询范围无效')
        personal.refresh_library()
        condition = "json_extract(data,'$.available')=1 AND json_extract(data,'$.alias_of') IS NULL"
        args = []
        if folder:
            condition += " AND EXISTS(SELECT 1 FROM json_each(json_extract(data,'$.folders')) WHERE value=?)"
            args.append(folder)
        if q:
            condition += " AND instr(casefold(json_extract(data,'$.title')||' '||json_extract(data,'$.artists')||' '||json_extract(data,'$.album')),?)>0"
            args.append(q.casefold())
        total = db.one('SELECT count(*) n FROM library_songs WHERE ' + condition, args)['n']
        rows = db.rows('SELECT data FROM library_songs WHERE ' + condition + ' ORDER BY id LIMIT ? OFFSET ?', [*args, limit, (page-1)*limit])
        return {'items': [json.loads(r['data']) for r in rows], 'total': total, 'page': page, 'cursor': personal.revision()}

    @router.get('/library/snapshot')
    def library_snapshot(after: str = '', cursor: int | None = None, limit: int = 500):
        if not 1 <= limit <= 500 or (after and not re.fullmatch(r'[a-f0-9]{32}', after)) or (cursor is not None and cursor < 0):
            raise ValueError('快照范围无效')
        personal.refresh_library()
        # Capture the watermark before reading pages. A final delta from this watermark
        # catches additions before the keyset boundary and edits made during pagination.
        watermark = personal.revision() if cursor is None else cursor
        rows = db.rows('SELECT id,data FROM library_songs WHERE id>? ORDER BY id LIMIT ?', (after, limit + 1))
        items = rows[:limit]
        return {'items': [json.loads(r['data']) for r in items], 'cursor': watermark,
                'after': items[-1]['id'] if items else after, 'has_more': len(rows) > limit}

    @router.get('/library/changes')
    def library_changes(cursor: int = 0):
        if cursor < 0:
            raise ValueError('游标无效')
        personal.refresh_library()
        rows = db.rows('SELECT seq,data FROM library_changes WHERE seq>? ORDER BY seq LIMIT 500', (cursor,))
        return {'items': [json.loads(r['data']) for r in rows], 'cursor': rows[-1]['seq'] if rows else cursor,
                'has_more': len(rows) == 500}

    @router.get('/library/songs/{song_id}')
    def song(song_id: str):
        personal.refresh_library()
        item = personal.song(song_id)
        a = db.one('SELECT path FROM assets WHERE id=?', (item['asset_id'],))
        path = within(cfg.music, a['path']) if a else None
        return {**item, 'lyrics': media.cached_lyrics(path) if path and path.is_file() else ''}

    @router.get('/library/playlists')
    def public_playlists():
        return {'items': [{'id': p['id'], 'name': p['name'], 'folder': p['folder'], 'platform': p['platform']}
                          for p in db.rows('SELECT * FROM playlists ORDER BY created DESC')]}

    @router.get('/library/playlists/{playlist_id}')
    def public_playlist(playlist_id: str):
        p = db.one('SELECT * FROM playlists WHERE id=?', (playlist_id,))
        if not p:
            raise HTTPException(404, '公共歌单不存在')
        personal.refresh_library()
        key = (playlist_id, personal.revision(), hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest())
        with public_lock:
            if key in public_cache:
                public_cache.move_to_end(key)
                return public_cache[key]
        tracks = engine.playlist_tracks(p)
        assets = [t['asset_id'] for t in tracks if t.get('asset_id')]
        songs = {r['asset_id']: r['id'] for r in db.rows('''SELECT m.asset_id,s.id FROM library_members m
            JOIN library_songs s ON s.identity=m.identity WHERE m.asset_id IN (SELECT value FROM json_each(?))''', (json.dumps(assets),))}
        result = {'id': p['id'], 'name': p['name'], 'song_ids': list(dict.fromkeys(songs[a] for a in assets if a in songs))}
        with public_lock:
            public_cache[key] = result
            while len(public_cache) > 32:
                public_cache.popitem(last=False)
        return result

    @router.get('/me/playlists')
    def playlists(request: Request):
        return {'items': personal.playlists(uid(request))}

    @router.get('/me/favorites')
    def favorites(request: Request):
        return {'song_ids': [r['song_id'] for r in db.rows('SELECT song_id FROM favorites WHERE user_id=? AND liked=1', (uid(request),))]}

    @router.get('/me/sync')
    def sync_get(request: Request, cursor: int = 0):
        if cursor < 0:
            raise ValueError('游标无效')
        return personal.changes(uid(request), cursor)

    @router.post('/me/sync')
    def sync_post(body: dict, request: Request):
        cursor = body.get('cursor', 0)
        if type(cursor) is not int or cursor < 0:
            raise ValueError('游标无效')
        personal.refresh_library()
        result = personal.apply(uid(request), body.get('operations', []))
        return {'results': result, **personal.changes(uid(request), cursor)}

    @router.post('/downloads/prepare')
    def prepare(body: dict):
        personal.refresh_library()
        return downloads.prepare(str(body.get('song_id', '')), str(body.get('profile', 'original')))

    @router.get('/downloads/{download_id}')
    def download(download_id: str):
        return downloads.status(download_id)

    @router.api_route('/downloads/{download_id}/audio', methods=['GET', 'HEAD'])
    def audio(download_id: str):
        path, data = downloads.path(download_id)
        return FileResponse(path, media_type=data['mime'], headers={'ETag': '"' + data['sha256'] + '"'},
                            filename=data['filename'], content_disposition_type='inline')

    releases = cfg.data / 'android-releases'

    @router.get('/releases/android')
    def android_release():
        manifest = releases / 'manifest.json'
        if not manifest.is_file():
            return {'available': False}
        try:
            data = json.loads(manifest.read_text())
            name = data['apk_url'].removeprefix('/downloads/android/')
            if not re.fullmatch(r'liusheng-[0-9A-Za-z._-]+\.apk', name):
                raise ValueError('APK 路径无效')
            path = within(releases, name)
            if not path.is_file() or path.stat().st_size != data['size']:
                raise ValueError('APK 不完整')
            if data['package_name'] != 'com.seafy.liusheng' or not re.fullmatch(r'[0-9a-f]{64}', data['sha256']):
                raise ValueError('更新清单无效')
            return {**data, 'available': True}
        except (ValueError, KeyError, OSError, TypeError):
            raise HTTPException(503, '更新包暂不可用，请稍后重试') from None

    @app.get('/downloads/android/{filename}')
    def apk(filename: str):
        if not re.fullmatch(r'liusheng-[0-9A-Za-z._-]+\.apk', filename):
            raise HTTPException(404)
        path = within(releases, filename)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type='application/vnd.android.package-archive', filename=filename)

    app.include_router(router)
