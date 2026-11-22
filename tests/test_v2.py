import hashlib
import json
import shutil
import time
import uuid

from fastapi.testclient import TestClient

from app.main import create_app
from test_service import cfg as config_fixture, tone as tone_fixture

cfg = config_fixture
tone = tone_fixture
PASSWORD = 'a-private-password-123'


def setup(app, cfg, client, tone=None):
    if tone:
        for name in ('one', 'two', 'three'):
            shutil.copyfile(tone, cfg.music / (name + '.flac'))
        app.state.engine.scan()
    password = (cfg.data / 'initial-password.txt').read_text().strip()
    result = client.post('/api/login', json={'username': 'admin', 'password': password}).json()
    client.headers['X-CSRF-Token'] = result['csrf']
    return password


def member(client, name):
    r = client.post('/api/v1/admin/users', json={'username': name, 'password': PASSWORD})
    assert r.status_code == 200, r.text
    result = client.post('/api/v1/auth/login', json={'username': name, 'password': PASSWORD, 'device_name': '测试手机'}).json()
    return {'Authorization': 'Bearer ' + result['token']}, result['user']['id']


def save(pid, ids, rev=0, name='我的歌单'):
    return {'id': uuid.uuid4().hex, 'kind': 'playlist.save', 'entity_id': pid, 'base_revision': rev,
            'payload': {'name': name, 'song_ids': ids}, 'device_name': '测试手机'}


def sync(client, headers, *ops):
    r = client.post('/api/v1/me/sync', json={'operations': list(ops), 'cursor': 0}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_migration_preserves_password_and_repeats_safely(cfg):
    app = create_app(cfg)
    with TestClient(app) as c:
        password = setup(app, cfg, c)
        old_cookie = c.cookies.get('music_session')
        uid = app.state.db.one('SELECT id FROM users')['id']
    app = create_app(cfg)
    with TestClient(app) as c:
        c.cookies.set('music_session', old_cookie)
        assert c.get('/api/session').json()['authenticated']
        assert c.post('/api/login', json={'username': 'admin', 'password': password}).status_code == 200
        assert app.state.db.one('SELECT count(*) n FROM users')['n'] == 1
        assert app.state.db.one('SELECT id FROM users')['id'] == uid


def test_shared_library_and_private_data_with_admin_boundary(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        a, _ = member(c, 'alice')
        b, _ = member(c, 'bob')
        songs = c.get('/api/v1/library', headers=a).json()['items']
        assert c.get('/api/v1/library', headers=b).json()['items'] == songs
        for method, path in [('GET', '/api/settings'), ('GET', '/api/jobs'), ('GET', '/api/search?q=x'),
                             ('GET', '/api/system'), ('POST', '/api/acquire'), ('POST', '/api/library/scan'),
                             ('POST', '/api/lx/import-url'), ('POST', '/api/playlists'), ('GET', '/api/v1/admin/users')]:
            assert c.request(method, path, headers=a, json={}).status_code == 403, path
        pid = uuid.uuid4().hex
        op = save(pid, [songs[0]['id']])
        sync(c, a, op)
        assert len(c.get('/api/v1/me/playlists', headers=a).json()['items']) == 1
        assert c.get('/api/v1/me/playlists', headers=b).json()['items'] == []
        assert c.post('/api/v1/me/sync', headers=b, json={'operations': [save(pid, [], 1)]}).status_code == 404
        assert c.get(songs[0]['audio_url'], headers=a).status_code == 200
        c.cookies.clear()
        assert c.get(songs[0]['audio_url']).status_code == 401


def test_operation_replay_and_collision(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        a, _ = member(c, 'alice')
        sid = c.get('/api/v1/library', headers=a).json()['items'][0]['id']
        op = save(uuid.uuid4().hex, [sid])
        first = sync(c, a, op)
        second = sync(c, a, op)
        assert first == second
        op['payload']['name'] = '另一份'
        assert c.post('/api/v1/me/sync', headers=a, json={'operations': [op]}).status_code == 409
        assert len(c.get('/api/v1/me/playlists', headers=a).json()['items']) == 1


def test_independent_additions_merge_and_reorder_conflicts_copy(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        a, _ = member(c, 'alice')
        x, y, z = [s['id'] for s in c.get('/api/v1/library', headers=a).json()['items']]
        pid = uuid.uuid4().hex
        sync(c, a, save(pid, [x]))
        sync(c, a, save(pid, [x, y], 1))
        merged = sync(c, a, save(pid, [x, z], 1))['results'][0]['playlist']
        assert merged['song_ids'] == [x, y, z]
        sync(c, a, save(pid, [z, y, x], 3))
        op = save(pid, [y, x, z], 3)
        result = sync(c, a, op)['results'][0]
        assert result['status'] == 'conflict'
        assert result['playlist']['conflict_of'] == pid
        assert result['playlist']['song_ids'] == [y, x, z]
        assert sync(c, a, op)['results'][0] == result
        assert len(c.get('/api/v1/me/playlists', headers=a).json()['items']) == 2


def test_deletion_edit_conflict_and_favorites_no_file_write(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        a, _ = member(c, 'alice')
        sid = c.get('/api/v1/library', headers=a).json()['items'][0]['id']
        pid = uuid.uuid4().hex
        sync(c, a, save(pid, [sid]))
        delete = {'id': uuid.uuid4().hex, 'kind': 'playlist.delete', 'entity_id': pid, 'base_revision': 1}
        sync(c, a, delete)
        result = sync(c, a, save(pid, [sid], 1, '离线更名'))['results'][0]
        assert result['status'] == 'conflict' and result['playlist']['id'] != pid
        favorite = {'id': uuid.uuid4().hex, 'kind': 'favorite.set', 'payload': {'song_id': sid, 'liked': True}}
        sync(c, a, favorite)
        assert c.get('/api/v1/me/favorites', headers=a).json()['song_ids'] == [sid]
        assert len(list(cfg.music.glob('*.flac'))) == 3
        assert app.state.db.one('SELECT count(*) n FROM jobs')['n'] == 0


def test_cookie_csrf_and_disabled_device_sessions(cfg):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c)
        a, uid = member(c, 'alice')
        assert c.get('/api/v1/me', headers=a).status_code == 200
        csrf = c.headers.pop('X-CSRF-Token')
        assert c.post('/api/v1/me/sync', json={'operations': []}).status_code == 403
        assert c.post('/api/v1/me/sync', json={'operations': []}, headers=a).status_code == 200
        c.headers['X-CSRF-Token'] = csrf
        assert c.patch('/api/v1/admin/users/' + uid, json={'enabled': False}).status_code == 200
        assert c.get('/api/v1/me', headers=a).status_code == 401


def test_original_download_range_checksum_and_restart(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        password = setup(app, cfg, c, tone)
        sid = c.get('/api/v1/library').json()['items'][0]['id']
        task = c.post('/api/v1/downloads/prepare', json={'song_id': sid}).json()
        deadline = time.monotonic() + 10
        while task['state'] in ('queued', 'preparing') and time.monotonic() < deadline:
            time.sleep(.02)
            task = c.get('/api/v1/downloads/' + task['id']).json()
        assert task['state'] == 'ready', task
        data = c.get(task['audio_url']).content
        assert hashlib.sha256(data).hexdigest() == task['sha256']
        assert data == tone.read_bytes()
        part = c.get(task['audio_url'], headers={'Range': 'bytes=5-24', 'If-Range': '"' + task['sha256'] + '"'})
        assert part.status_code == 206 and part.content == data[5:25]
        assert c.get(task['audio_url'], headers={'Range': 'bytes=5-', 'If-Range': '"old"'}).status_code == 200
    app = create_app(cfg)
    with TestClient(app) as c:
        c.post('/api/login', json={'password': password})
        assert c.get(task['audio_url']).content == data
        assert c.get('/api/v1/downloads/' + task['id']).json()['state'] == 'ready'


def test_song_identity_and_missing_marker(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        old = c.get('/api/v1/library').json()
        assert c.get('/api/v1/library').json() == old
        app.state.db.execute('UPDATE assets SET present=0')
        changed = c.get('/api/v1/library/changes', params={'cursor': old['cursor']}).json()
        assert len(changed['items']) == 3 and all(not s['available'] for s in changed['items'])


def test_update_manifest_is_public_but_bounded(cfg):
    app = create_app(cfg)
    with TestClient(app) as c:
        assert c.get('/api/v1/releases/android').json() == {'available': False}
        root = cfg.data / 'android-releases'
        root.mkdir()
        (root / 'liusheng-2.0.0-1.apk').write_bytes(b'test-only-not-installable')
        manifest = {'apk_url': '/downloads/android/liusheng-2.0.0-1.apk', 'size': 24,
                    'sha256': '0' * 64, 'package_name': 'com.seafy.liusheng', 'version_code': 1}
        manifest['size'] = (root / 'liusheng-2.0.0-1.apk').stat().st_size
        (root / 'manifest.json').write_text(json.dumps(manifest))
        assert c.get('/api/v1/releases/android').json()['available']
        assert c.get(manifest['apk_url']).status_code == 200
        manifest['apk_url'] = '/downloads/android/../music.sqlite3'
        (root / 'manifest.json').write_text(json.dumps(manifest))
        assert c.get('/api/v1/releases/android').status_code == 503


def test_real_legacy_auth_migration_revokes_only_old_sessions(cfg):
    from app.accounts import password_hash
    from app.db import Database, now
    cfg.prepare()
    db = Database(cfg.data / 'music.sqlite3')
    salt = '12' * 16
    db.set('auth', {'salt': salt, 'hash': password_hash(PASSWORD, salt)})
    db.execute('INSERT INTO sessions(token,csrf,expires) VALUES (?,?,?)',
               (hashlib.sha256(b'old-session').hexdigest(), 'old-csrf', now() + 3600))
    app = create_app(cfg)
    with TestClient(app) as c:
        c.cookies.set('music_session', 'old-session')
        assert not c.get('/api/session').json()['authenticated']
        result = c.post('/api/login', json={'username': 'admin', 'password': PASSWORD})
        assert result.status_code == 200
        assert app.state.db.one('SELECT password_hash FROM users')['password_hash'] == password_hash(PASSWORD, salt)


def test_source_enrichment_and_mapping_preserve_personal_song_references(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        songs = c.get('/api/v1/library').json()['items']
        x, y = songs[:2]
        assert x['folders'] == ['根目录']
        app.state.db.execute('UPDATE assets SET track_key=? WHERE id=?', ('netease:123', x['asset_id']))
        refreshed = c.get('/api/v1/library').json()['items']
        assert next(s for s in refreshed if s['asset_id'] == x['asset_id'])['id'] == x['id']
        app.state.db.execute('UPDATE assets SET track_key=? WHERE id=?', ('qq:456', y['asset_id']))
        c.get('/api/v1/library')
        app.state.db.execute('INSERT INTO mappings(original,target,created) VALUES (?,?,?)', ('netease:123', 'qq:456', int(time.time())))
        assert len(c.get('/api/v1/library').json()['items']) == 2
        old = c.get('/api/v1/library/songs/' + x['id']).json()
        assert old['available'] and old['alias_of'] == y['id']
        assert c.get(old['audio_url']).status_code == 200


def test_compatible_download_and_changed_source_use_distinct_versions(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        song = c.get('/api/v1/library').json()['items'][0]

        def ready(profile):
            task = c.post('/api/v1/downloads/prepare', json={'song_id': song['id'], 'profile': profile}).json()
            deadline = time.monotonic() + 10
            while task['state'] in ('queued', 'preparing') and time.monotonic() < deadline:
                time.sleep(.02)
                task = c.get('/api/v1/downloads/' + task['id']).json()
            assert task['state'] == 'ready', task
            return task

        first = ready('original')
        compatible = ready('compatible')
        assert compatible['profile'] == 'compatible' and compatible['filename'].endswith('.mp3')
        assert compatible['sha256'] != first['sha256']
        path = cfg.music / app.state.db.one('SELECT path FROM assets WHERE id=?', (song['asset_id'],))['path']
        path.touch()
        changed = ready('original')
        assert changed['id'] != first['id']
        assert c.get(first['audio_url']).content == tone.read_bytes()


def test_client_rebase_conflict_keeps_authoritative_server_snapshot(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as c:
        setup(app, cfg, c, tone)
        a, _ = member(c, 'alice')
        x, y, z = [s['id'] for s in c.get('/api/v1/library', headers=a).json()['items']]
        pid = uuid.uuid4().hex
        sync(c, a, save(pid, [x, y]))
        op = save(pid, [y, x, z], 1, '本地后续排序')
        op['conflict_copy'] = True
        result = sync(c, a, op)['results'][0]
        assert result['server_playlist']['song_ids'] == [x, y]
        assert result['playlist']['song_ids'] == [y, x, z]
        assert sync(c, a, op)['results'][0] == result
