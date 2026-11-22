"""Query complexity, incremental identity correctness and cache invalidation."""
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app import media
from app.db import dumps
from app.main import create_app
from test_v2 import setup, cfg as config_fixture, tone as tone_fixture

cfg = config_fixture
tone = tone_fixture


def seed(db, count=100, folder='大歌单'):
    with db.connect() as c:
        c.executemany('INSERT INTO assets VALUES (?,?,?,?,?, ?,NULL,1,0)', [
            (f'{i:032x}', f'{folder}/{i}.flac', None, dumps({'title': str(i), 'artists': ['样本'], 'album': '合成', 'duration': 10}), 100, 1)
            for i in range(count)])


def test_index_no_change_does_not_write_or_scan_and_one_change_is_local(cfg):
    app = create_app(cfg)
    db, personal = app.state.db, app.state.personal
    seed(db, 1000)
    personal.refresh_library()
    revision = personal.revision()
    statements = []
    original = db.connect

    @contextmanager
    def trace():
        with original() as c:
            c.set_trace_callback(statements.append)
            yield c
    db.connect = trace
    personal.refresh_library()
    assert not any('BEGIN IMMEDIATE' in s or 'FROM assets' in s or s.startswith('UPDATE') for s in statements)
    db.execute("UPDATE assets SET data=json_set(data,'$.title','新标题') WHERE id=?", ('0' * 32,))
    statements.clear()
    personal.refresh_library()
    assert personal.revision() == revision + 1
    assert not any('SELECT * FROM assets ORDER BY' in s or 'SELECT * FROM library_songs' == s for s in statements)
    # Recreating the app preserves the incremental checkpoint rather than rebuilding all songs.
    assert create_app(cfg).state.personal.revision() == personal.revision()


def test_incremental_identity_enrichment_duplicate_and_delete(cfg):
    app = create_app(cfg)
    db, personal = app.state.db, app.state.personal
    seed(db, 2)
    personal.refresh_library()
    sid = db.one('SELECT id FROM library_songs WHERE identity=?', ('local:' + '0'*32,))['id']
    db.execute('UPDATE assets SET track_key=? WHERE id=?', ('netease:1', '0'*32))
    personal.refresh_library()
    assert db.one("SELECT id FROM library_songs WHERE identity='track:netease:1'")['id'] == sid
    db.execute('UPDATE assets SET track_key=? WHERE id=?', ('netease:1', f'{1:032x}'))
    personal.refresh_library()
    assert personal.song(sid)['available']
    db.execute('DELETE FROM assets WHERE id=?', ('0'*32,))
    personal.refresh_library()
    assert personal.song(sid)['asset_id'] == f'{1:032x}'
    db.execute('DELETE FROM assets')
    personal.refresh_library()
    assert not personal.song(sid)['available']


def test_snapshot_catches_changes_during_pagination(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        setup(app, cfg, client)
        seed(app.state.db, 4)
        first = client.get('/api/v1/library/snapshot?limit=2').json()
        observed = {s['id']: s for s in first['items']}
        app.state.db.execute("UPDATE assets SET data=json_set(data,'$.title','已变化')")
        after = first['after']
        while True:
            page = client.get('/api/v1/library/snapshot', params={'after': after, 'cursor': first['cursor'], 'limit': 2}).json()
            observed.update({s['id']: s for s in page['items']})
            after = page['after']
            if not page['has_more']:
                break
        changes = client.get('/api/v1/library/changes', params={'cursor': first['cursor']}).json()
        observed.update({s['id']: s for s in changes['items']})
        assert len(observed) == 4
        assert all(s['title'] == '已变化' for s in observed.values())
        assert all(s['revision'] > first['cursor'] for s in observed.values())


def test_large_public_playlist_has_bounded_queries_and_invalidates(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        setup(app, cfg, client)
        seed(app.state.db, 3000)
        pid = app.state.engine.local_playlist('大歌单')
        app.state.personal.refresh_library()
        calls = []
        original = app.state.db.rows
        def counted(sql, args=()):
            calls.append(sql)
            return original(sql, args)
        app.state.db.rows = counted
        first = client.get('/api/v1/library/playlists/' + pid).json()
        assert len(first['song_ids']) == 3000
        assert len(calls) < 15
        calls.clear()
        assert client.get('/api/v1/library/playlists/' + pid).json() == first
        assert not any('JOIN library_songs' in s for s in calls)
        app.state.db.execute('UPDATE assets SET present=0 WHERE id=?', ('0'*32,))
        assert len(client.get('/api/v1/library/playlists/' + pid).json()['song_ids']) == 2999
        assert client.get('/api/playlists').json()['items'][0]['available'] == 2999


def test_lyrics_cache_invalidates_sidecar_and_cover_requires_session(cfg, tone):
    app = create_app(cfg)
    with TestClient(app) as client:
        setup(app, cfg, client, tone)
        path = cfg.music / 'one.flac'
        path.with_suffix('.lrc').write_text('[00:01.00]原歌词')
        assert '原歌词' in media.cached_lyrics(path)
        path.with_suffix('.lrc').write_text('[00:01.00]更新后的歌词')
        assert '更新后的歌词' in media.cached_lyrics(path)
        asset = app.state.db.one('SELECT id FROM assets')
        # Existing extracted artwork avoids changing a music file for this HTTP cache test.
        (cfg.data / 'covers' / (asset['id']+'.jpg')).write_bytes(b'cover-fixture')
        url = '/api/covers/' + asset['id']
        response = client.get(url)
        assert response.headers['cache-control'] == 'private, no-cache'
        assert client.get(url, headers={'If-None-Match': response.headers['etag']}).status_code == 304
        client.cookies.clear()
        assert client.get(url, headers={'If-None-Match': response.headers['etag']}).status_code == 401
