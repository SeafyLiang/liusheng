import json
import shutil
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app import media
from app.db import Database, now
from app.lx import LXManager
from app.main import create_app
from app.public_http import PublicHTTPError, public_target
from app.sources import SourceError, Sources
from lx_runner.server import run_script
from test_service import cfg as config_fixture, tone as tone_fixture, track

cfg = config_fixture
tone = tone_fixture

SCRIPT = b"""/** @name Example */
lx.on(lx.EVENT_NAMES.request, async () => 'https://music.126.net/sample.mp3');
lx.send(lx.EVENT_NAMES.inited,{sources:{wy:{actions:['musicUrl'],qualitys:['128k']}}});"""


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.100", "169.254.169.254", "198.18.0.1", "::1", "::ffff:127.0.0.1"])
def test_lx_denies_internal_dns(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(PublicHTTPError):
        public_target("https://source.example/script")


def test_lx_rechecks_redirect_destination(monkeypatch):
    from app import public_http

    class Response:
        status = 302

        def getheader(self, key):
            return "http://127.0.0.1/admin"

    class Connection:
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): pass
        def getresponse(self): return Response()
        def close(self): pass

    class Socket:
        def close(self): pass

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34" if host == "example.org" else "127.0.0.1", 80))])
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: Socket())
    monkeypatch.setattr(public_http.http.client, "HTTPConnection", Connection)
    with pytest.raises(PublicHTTPError):
        public_http.open_public("http://example.org/script")


def test_runner_protocol_and_host_isolation():
    script = SCRIPT.decode().replace("async () => 'https://music.126.net/sample.mp3'",
                                    "async () => { if(typeof process!=='undefined'||typeof require!=='undefined') throw Error('host'); return 'https://music.126.net/sample.mp3' }")
    result = run_script({"script": script, "request": {"action": "musicUrl", "source": "wy", "info": {}}})
    assert result["ok"] and result["url"] == "https://music.126.net/sample.mp3"


def test_runner_rejects_local_request():
    script = SCRIPT.decode().replace("async () => 'https://music.126.net/sample.mp3'", "() => new Promise((resolve,reject)=>lx.request('http://127.0.0.1/',{},(err,res)=>err?reject(err):resolve(res.body)))")
    result = run_script({"script": script, "request": {"action": "musicUrl", "source": "wy", "info": {}}})
    assert not result["ok"] and result["requests"][0]["error"] == "blocked_address"


def test_runner_bounds_infinite_script():
    result = run_script({"script": "while(true) {}"})
    # CPU quota and wall-clock deadline both terminate the child. A heavily loaded
    # host may hit the wall-clock deadline before accumulating six CPU seconds.
    assert result["ok"] is False
    assert result["code"] in {"resource_limit", "timeout"}


def test_lx_import_duplicate_integrity_and_enable_guard(cfg, monkeypatch):
    app = create_app(cfg)
    manager = app.state.lx
    ident = manager.import_script(SCRIPT)["id"]
    assert manager.import_script(SCRIPT)["duplicate"]
    assert not manager.eligible()
    assert (manager.directory / (ident + ".js")).stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("LX_RUNNER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LX_RUNNER_TOKEN", "test")
    (manager.directory / (ident + ".js")).write_text("changed")
    with pytest.raises(SourceError, match="重新导入"):
        manager.run(ident)
    with TestClient(app) as client:
        assert client.post('/api/lx/import-url', json={'url': 'http://127.0.0.1/'}).status_code == 401
        login = client.post('/api/login', json={'password': (cfg.data / 'initial-password.txt').read_text().strip()})
        headers = {'X-CSRF-Token': login.json()['csrf']}
        assert client.post('/api/lx/import-file', files={'file': ('source.js', SCRIPT)}).status_code == 403
        assert client.patch('/api/settings', json={'providers': ['lx:' + ident]}, headers=headers).status_code == 400
        assert client.patch('/api/settings', json={'providers': []}, headers=headers).status_code == 200
        assert client.patch('/api/settings', json={'providers': [{}]}, headers=headers).status_code == 400


def test_lx_probe_full_audio_and_batch_restriction(cfg, tone, monkeypatch):
    db = Database(cfg.data / 'lx-test.sqlite3')
    manager = LXManager(cfg, db)
    ident = manager.import_script(SCRIPT)["id"]
    monkeypatch.setattr(manager, 'run', lambda _, request=None: {'ok': True, 'url': 'https://example.org/audio', 'capabilities': {'wy': {'qualitys': ['128k'], 'actions': ['musicUrl']}}})
    monkeypatch.setattr('app.lx.download_public', lambda url, target, **kw: shutil.copyfile(tone, target))
    result = manager.probe(ident, track())
    assert result['ok'] and result['evidence']['full_decode']
    assert manager.eligible() == ['lx:' + ident]
    assert list(cfg.music.iterdir()) == [] and list((cfg.data / 'staging').iterdir()) == []
    restricted = manager.import_script(SCRIPT.replace(b'Example', 'Limited\n * @description 禁止批量下载'.encode()))['id']
    assert manager.probe(restricted, track())['ok']
    assert 'lx:' + restricted not in manager.eligible()
    with pytest.raises(SourceError, match='批量'):
        manager.resolve(restricted, track())
    db.set('providers', ['gd', 'lx:' + ident])
    monkeypatch.setattr(media, 'probe', lambda *a, **kw: (_ for _ in ()).throw(ValueError('wrong duration')))
    assert not manager.probe(ident, track())['ok']
    assert db.get('providers') == ['gd']
    assert not manager.eligible()


def test_lx_preserves_platform_ids_and_rejects_unverified_platform(cfg, monkeypatch):
    manager = LXManager(cfg, Database(cfg.data / 'source.sqlite3'))
    ident = manager.import_script(SCRIPT)['id']
    manager.db.execute("UPDATE lx_sources SET capabilities=?,state='verified',verified_platforms='[\"wy\"]' WHERE id=?",
                       (json.dumps({'wy': {'qualitys': ['128k']}, 'tx': {'qualitys': ['128k']}}), ident))
    seen = []
    monkeypatch.setattr(manager, 'run', lambda _, request=None: seen.append(request) or {'ok': True, 'url': 'https://example.org/audio'})
    manager.resolve(ident, track('netease:123'))
    assert seen[0]['source'] == 'wy' and seen[0]['info']['musicInfo']['songmid'] == '123'
    with pytest.raises(SourceError) as exc:
        manager.resolve(ident, track('tencent:123'))
    assert exc.value.code == 'not_verified'


def test_qq_direct_is_independent_and_no_url_requires_auth(cfg):
    source = Sources(Database(cfg.data / 'source.sqlite3'))
    source.client.close()
    calls = []

    def respond(request):
        calls.append(request)
        payload = json.loads(request.content)
        files = payload['req_1']['param']['filename']
        return httpx.Response(200, json={'code': 0, 'req_1': {'code': 0, 'data': {'midurlinfo': [{'filename': files[-1], 'purl': 'test.mp3?vkey=private'}]}}})

    source.client = httpx.Client(transport=httpx.MockTransport(respond))
    result = source.audio_url(track('tencent:mid'), 'tencent_direct')
    assert result['quality'] == '128k' and all(r.url.host == 'u.y.qq.com' for r in calls)
    with pytest.raises(SourceError) as exc:
        source.audio_url(track(), 'tencent_direct')
    assert exc.value.code == 'unsupported'
    source.client.close()
    source.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'code': 0, 'req_1': {'code': 0, 'data': {'midurlinfo': []}}})))
    with pytest.raises(SourceError) as exc:
        source.audio_url(track('tencent:mid'), 'tencent_direct')
    assert exc.value.code == 'auth_required'
    source.close()


def test_lrclib_fallback_matching_and_evidence(cfg, monkeypatch):
    source = Sources(Database(cfg.data / 'lyrics.sqlite3'))
    source.db.set('lyric_providers', ['platform', 'lrclib'])
    monkeypatch.setattr(source, 'platform_lyrics', lambda _: (_ for _ in ()).throw(SourceError('平台暂不可用')))
    data = {'trackName': '回声 (Live)', 'artistName': '样本歌手', 'albumName': '验证专辑', 'duration': 10,
            'syncedLyrics': '[00:00.00]第一句\n[00:03.00]第二句\n[00:06.00]第三句'}
    monkeypatch.setattr(source, 'get', lambda *a, **kw: data)
    result = source.lyrics(track())
    assert result['_source'] == 'lrclib' and result['_attempts'][0]['status'] == 'unavailable'
    data['trackName'] = '回声'
    assert source.lyrics(track())['_attempts'][-1]['status'] == 'version_mismatch'
    data['trackName'] = '回声 (Live)'
    data['duration'] = 40
    assert source.lyrics(track())['_attempts'][-1]['status'] == 'version_mismatch'
    source.close()


def test_lrclib_does_not_replace_existing_platform_lyrics(cfg, monkeypatch):
    source = Sources(Database(cfg.data / 'lyrics.sqlite3'))
    monkeypatch.setattr(source, 'platform_lyrics', lambda _: {'nolyric': True})
    monkeypatch.setattr(source, 'lrclib_lyrics', lambda _: pytest.fail('should not request fallback'))
    assert source.lyrics(track())['nolyric'] is True
    source.db.set('lyric_providers', [])
    assert source.lyrics(track())['_attempts'] == []
    source.close()


def test_lx_unavailable_and_cooldown_are_not_not_found(cfg, monkeypatch):
    manager = LXManager(cfg, Database(cfg.data / 'source.sqlite3'))
    ident = manager.import_script(SCRIPT)['id']
    manager.db.execute("UPDATE lx_sources SET capabilities=? WHERE id=?", (json.dumps({'wy': {'qualitys': ['128k']}}), ident))
    monkeypatch.setattr(manager, 'run', lambda *a, **kw: {'ok': False, 'code': 'script_rejected', 'requests': [{'status': 429}]})
    with pytest.raises(SourceError) as exc:
        manager.resolve(ident, track(), testing=True)
    assert exc.value.code == 'rate_limited'
    assert manager.db.one('SELECT blocked_until FROM source_health')['blocked_until'] > now()
    with pytest.raises(SourceError) as exc:
        manager.resolve(ident, track(), testing=True)
    assert exc.value.code == 'cooldown'


def test_one_platform_failure_preserves_other_verified_platform(cfg):
    manager = LXManager(cfg, Database(cfg.data / 'source.sqlite3'))
    ident = manager.import_script(SCRIPT)['id']
    manager.db.execute("UPDATE lx_sources SET state='verified',verified_platforms='[\"wy\",\"tx\"]' WHERE id=?", (ident,))
    manager.db.set('providers', ['lx:' + ident])
    manager.fail(ident, SourceError('该平台失败'), {'track': 'netease:123'})
    assert json.loads(manager.item(ident)['verified_platforms']) == ['tx']
    assert manager.db.get('providers') == ['lx:' + ident]
    assert manager.item(ident)['state'] == 'verified'
