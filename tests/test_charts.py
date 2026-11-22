import base64
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.charts import APPLE_ID, SPOTIFY_ID, apple_playlist, spotify_playlist
from app.db import Database
from app.engine import Engine
from app.matching import same_recording
from app.sources import SourceError, Sources
from app.subscriptions import ZONE, merge_members, next_check
from test_service import cfg, track  # noqa: F401


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZONE).timestamp())


@pytest.mark.parametrize('start,expected', [('2026-11-22T23:59', '2026-11-23T04:00'),
    ('2026-11-23T03:59', '2026-11-23T04:00'), ('2026-11-23T04:00', '2026-11-30T04:00'),
    ('2026-12-31T12:00', '2027-01-04T04:00')])
def test_weekly_time(start, expected):
    assert next_check('weekly_monday_0400', stamp(start)) == stamp(expected)


def test_append_preserves_order_and_removed():
    a, b, c = [track('netease:' + str(i)) for i in range(3)]
    assert merge_members([a, b], [c, b], 'append') == [a, b, c]
    assert merge_members([a, b, c], [b, a], 'append') == [a, b, c]


def test_weekly_sync_retention_and_retry(cfg, monkeypatch):
    db = Database(cfg.data / 'test.sqlite3')
    a, b, c = [track('netease:' + str(i)) for i in range(3)]
    initial = {'platform': 'netease', 'source_id': 'test', 'name': '热歌', 'url': '', 'tracks': [a, b]}
    engine = Engine(cfg, db, SimpleNamespace(playlist=lambda *_: {**initial, 'tracks': [c, b]}))
    monkeypatch.setattr(engine, 'export', lambda *_: None)
    pid = engine.add_playlist(initial, schedule='weekly_monday_0400', retention='append', auto_add=True)['id']
    db.execute('INSERT INTO subscription_acquired VALUES(?,?)', (pid, a['key']))
    queued = []
    monkeypatch.setattr(engine, 'queue_missing', lambda tracks, *_: queued.extend(t['key'] for t in tracks) or queued)
    jid = engine.enqueue('sync', {'playlist_id': pid}, 'test')
    engine.sync(jid, pid)
    p = db.one('SELECT * FROM playlists WHERE id=?', (pid,))
    assert [t['key'] for t in json.loads(p['snapshot'])] == [a['key'], b['key'], c['key']]
    assert [t['key'] for t in json.loads(p['latest_snapshot'])] == [c['key'], b['key']]
    assert queued == [b['key'], c['key']]  # 未成功旧曲重试；已完成不重下
    assert p['next_check'] == next_check(p['schedule'], p['last_check'])
    before = p['snapshot']
    engine.sources.playlist = lambda *_: (_ for _ in ()).throw(SourceError('incomplete', 'incomplete'))
    with pytest.raises(SourceError):
        engine.sync(jid, pid)
    assert db.one('SELECT snapshot FROM playlists WHERE id=?', (pid,))['snapshot'] == before


def test_schedule_catchup_idempotent(cfg):
    db = Database(cfg.data / 'test.sqlite3');engine = Engine(cfg, db, None)
    db.execute("INSERT INTO playlists(id,platform,source_id,name,source_url,enabled,auto_add,snapshot,created,schedule,next_check) VALUES('tencent:chart:26','tencent','chart:26','QQ','',1,1,'[]',0,'weekly_monday_0400',1)")
    engine.schedule();engine.schedule()
    assert len(db.rows("SELECT * FROM jobs WHERE kind='sync'")) == 1
    assert json.loads(db.one("SELECT payload FROM jobs WHERE kind='sync'")['payload'])['weekly_due'] is True


@pytest.mark.parametrize('change', [{'title': '回声'}, {'artists': ['其他歌手']}, {'duration': 13}, {'isrc': 'DIFFERENT'}])
def test_match_rejects_ambiguity(change):
    a = {**track(), 'isrc': 'SAME'}
    assert not same_recording(a, {**a, **change})


def test_match_exact():
    a = track();assert not same_recording(a, {**a, 'album': '不同专辑'}); assert same_recording(a, {**a, 'duration': 11})
    assert not same_recording(a, {**a, 'duration': 0})


def test_resolve_chart_urls(cfg):
    source = Sources(Database(cfg.data / 'test.sqlite3'))
    try:
        assert source.resolve('https://y.qq.com/n/ryqq/toplist/26') == ('tencent', 'chart:26')
        assert source.resolve('https://music.apple.com/cn/playlist/' + APPLE_ID) == ('apple', APPLE_ID)
        assert source.resolve('https://open.spotify.com/playlist/' + SPOTIFY_ID) == ('spotify', SPOTIFY_ID)
    finally:source.close()


def test_spotify_rejects_embed_truncation():
    uri = 'spotify:track:' + 'A' * 22
    entity = {'name': 'Mandopop Hits', 'ownerV2': {'data': {'username': 'spotify'}},
              'content': {'totalCount': 51, 'items': []}}
    state = {'entities': {'items': {'spotify:playlist:' + SPOTIFY_ID: entity}}}
    embed = {'props': {'pageProps': {'state': {'data': {'entity': {'id': SPOTIFY_ID, 'name': 'Mandopop Hits',
              'trackList': [{'uri': uri, 'title': '歌', 'subtitle': '人', 'duration': 200000}]}}}}}}
    page = '<script id="initialState">' + base64.b64encode(json.dumps(state).encode()).decode() + '</script>'
    small = '<script id="__NEXT_DATA__">' + json.dumps(embed) + '</script>'
    source = SimpleNamespace(request=lambda url: SimpleNamespace(text=small if '/embed/' in url else page))
    with pytest.raises(SourceError, match='不完整'):
        spotify_playlist(source, SPOTIFY_ID)


def test_apple_redirect_rejected():
    with pytest.raises(SourceError, match='跳转'):
        apple_playlist(SimpleNamespace(request=lambda _: SimpleNamespace(url='https://music.apple.com/cn/new')), APPLE_ID)


def test_settings_api_preserves_schedule(cfg):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(cfg)
    with TestClient(app) as client:
        auth = client.post('/api/login', json={'password': (cfg.data / 'initial-password.txt').read_text().strip()}).json()
        headers = {'X-CSRF-Token': auth['csrf']}
        pid = app.state.engine.add_playlist({'platform': 'netease', 'source_id': '123', 'name': '榜', 'url': '', 'tracks': [track()]},
                                             schedule='weekly_monday_0400', retention='append')['id']
        before = client.get('/api/playlists/' + pid).json()
        assert before['current_count'] == 1
        assert 'latest_snapshot' not in before
        assert client.patch('/api/playlists/' + pid, headers=headers, json={'auto_add': True}).status_code == 200
        after = client.get('/api/playlists/' + pid).json()
        assert after['next_check'] == before['next_check']
        assert client.patch('/api/playlists/' + pid, headers=headers, json={'schedule': 'hourly'}).status_code == 400
        assert client.patch('/api/playlists/' + pid, headers=headers, json={'schedule': 'manual'}).status_code == 200
        assert not client.get('/api/playlists/' + pid).json()['enabled']
        assert len(client.get('/api/chart-sources').json()['items']) == 5


def test_external_review_does_not_distribute(cfg, monkeypatch):
    db = Database(cfg.data / 'review.sqlite3')
    original = track('apple:123')
    db.save_track(original)
    engine = Engine(cfg, db, SimpleNamespace(search=lambda *_: []))
    pid = engine.add_playlist({'platform': 'apple', 'source_id': APPLE_ID, 'name': '榜', 'url': '', 'tracks': [original]},
                              schedule='weekly_monday_0400', retention='append')['id']
    jid = engine.queue_track(original, pid)
    monkeypatch.setattr(engine, 'finish_distribution', lambda *_: pytest.fail('待匹配不应执行分发'))
    engine.run_job(db.one('SELECT * FROM jobs WHERE id=?', (jid,)))
    assert db.one('SELECT state FROM jobs WHERE id=?', (jid,))['state'] == 'review'
    assert not db.rows('SELECT * FROM mappings')


def test_ambiguous_library_never_falls_through_to_search(cfg):
    from app.matching import resolve
    db = Database(cfg.data / 'ambiguous.sqlite3')
    original = track('apple:123')
    rows = [{'data': json.dumps(track('netease:1'))}, {'data': json.dumps(track('netease:2'))}]
    class FakeDB:
        def rows(self, *_):return rows
        def execute(self, sql, args):return db.execute(sql, args)
    engine = SimpleNamespace(db=FakeDB(), canonical=lambda key: key,
                             sources=SimpleNamespace(search=lambda *_: pytest.fail('歧义不可用搜索结果掩盖')))
    assert resolve(engine, original) is None
    assert db.one('SELECT state FROM track_matches')['state'] == 'review'
