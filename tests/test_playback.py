import shutil
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import media
from app.main import create_app
from app.playback import MIB
from app.sources import SourceError
from test_service import cfg as config_fixture, tone as tone_fixture, track

cfg = config_fixture
tone = tone_fixture


def login(c, cfg):
    r = c.post("/api/login", json={"password": (cfg.data / "initial-password.txt").read_text().strip()})
    return {"X-CSRF-Token": r.json()["csrf"]}


def wait_ready(c, ident):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = c.get("/api/playback/preparations/" + ident).json()
        if result["state"] != "preparing":
            return result
        time.sleep(0.02)
    pytest.fail("preparation timed out")


def seed(app, cfg, tone):
    shutil.copyfile(tone, cfg.music / "sample.flac")
    app.state.engine.scan()
    return app.state.db.one("SELECT * FROM assets")["id"]


def test_audio_range_head_auth_and_bounds(cfg, tone):
    app = create_app(cfg)
    ident = seed(app, cfg, tone)
    path = "/api/library/" + ident + "/audio"
    with TestClient(app) as c:
        assert c.get(path).status_code == 401
        login(c, cfg)
        r = c.get(path, headers={"Range": "bytes=10-29"})
        assert r.status_code == 206 and r.content == tone.read_bytes()[10:30]
        assert r.headers["content-range"] == f"bytes 10-29/{tone.stat().st_size}"
        assert r.headers["content-type"] == "audio/flac"
        assert r.headers["cache-control"] == "no-store"
        assert c.head(path).content == b""
        assert int(c.head(path).headers["content-length"]) == tone.stat().st_size
        assert c.get(path, headers={"Range": "bytes=9999999999-"}).status_code == 416
        assert c.get(path, headers={"Range": "bytes=-12"}).content == tone.read_bytes()[-12:]
        assert c.get("/api/library/nonexistent/audio").status_code == 404
        app.state.db.execute("UPDATE assets SET path=? WHERE id=?", ("../private.flac", ident))
        assert c.get(path).status_code == 400


def test_compatibility_copy_preserves_original_and_requires_csrf(cfg, tone):
    app = create_app(cfg)
    ident = seed(app, cfg, tone)
    before = media.sha(cfg.music / "sample.flac")
    with TestClient(app) as c:
        headers = login(c, cfg)
        assert c.post("/api/playback/prepare", json={"asset_id": ident}).status_code == 403
        r = c.post("/api/playback/prepare", json={"asset_id": ident}, headers=headers).json()
        r = wait_ready(c, r["id"])
        assert r["state"] == "ready" and r["source"] == "local"
        assert c.get(r["url"], headers={"Range": "bytes=0-31"}).status_code == 206
        assert media.sha(cfg.music / "sample.flac") == before
        assert app.state.db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0
        assert c.post("/api/playback/prepare", json={"asset_id": ident}, headers=headers).json()["state"] == "ready"
        assert c.get("/api/playback/cache/unknown").status_code == 404
        c.cookies.clear()
        assert c.get(r["url"]).status_code == 401


def test_preview_fallback_partial_no_library_write(cfg, tone):
    app = create_app(cfg)
    s = app.state.sources
    app.state.db.save_track(track(duration=200))
    app.state.db.set("providers", ["netease_direct", "gd"])
    calls = []

    def resolve(t, provider, bitrate=999):
        calls.append((provider, bitrate))
        if provider == "netease_direct":
            raise SourceError("来源拒绝访问", "auth_required")
        return {"url": "https://music.163.com/test"}

    s.audio_url = resolve
    s.download = lambda url, dest, cancel, max_bytes: shutil.copyfile(tone, dest)
    with TestClient(app) as c:
        headers = login(c, cfg)
        r = c.post("/api/playback/prepare", json={"key": "netease:101"}, headers=headers).json()
        r = wait_ready(c, r["id"])
        assert r["state"] == "ready" and r["partial"] is True
        assert calls == [("netease_direct", 128), ("gd", 128)]
        assert r["attempts"][0]["status"] == "auth_required"
        assert "https://" not in str(r)
        assert c.head(r["url"]).status_code == 200
        assert app.state.db.one("SELECT COUNT(*) n FROM assets")["n"] == 0
        assert app.state.db.one("SELECT COUNT(*) n FROM jobs")["n"] == 0
        assert not list(cfg.music.iterdir())


def test_preview_reuses_existing_library(cfg, tone):
    app = create_app(cfg)
    ident = seed(app, cfg, tone)
    app.state.db.save_track(track())
    app.state.db.execute("UPDATE assets SET track_key=? WHERE id=?", ("netease:101", ident))
    with TestClient(app) as c:
        headers = login(c, cfg)
        r = c.post("/api/playback/prepare", json={"key": "netease:101"}, headers=headers).json()
        assert r["state"] == "ready" and r["asset_id"] == ident
        assert r["url"].endswith(ident + "/audio")
        assert not app.state.playback.entries
        assert c.post("/api/playback/prepare", json={"url": "http://127.0.0.1"}, headers=headers).status_code == 404


def test_preview_single_preparation_and_failure(cfg):
    app = create_app(cfg)
    entered = threading.Event()
    release = threading.Event()
    for k in ["netease:101", "netease:102"]:
        app.state.db.save_track(track(k))
    app.state.db.set("providers", ["gd"])

    def resolve(*args, **kwargs):
        entered.set()
        release.wait(5)
        raise SourceError("来源限流", "rate_limited")

    app.state.sources.audio_url = resolve
    with TestClient(app) as c:
        headers = login(c, cfg)
        r = c.post("/api/playback/prepare", json={"key": "netease:101"}, headers=headers).json()
        assert entered.wait(2)
        try:
            same = c.post("/api/playback/prepare", json={"key": "netease:101"}, headers=headers)
            assert same.status_code == 200 and same.json()["id"] == r["id"]
            assert c.post("/api/playback/prepare", json={"key": "netease:102"}, headers=headers).status_code == 429
        finally:
            release.set()
        result = wait_ready(c, r["id"])
        assert result["state"] == "failed" and result["attempts"][0]["status"] == "rate_limited"
        assert not list(app.state.playback.root.iterdir())


def test_cache_expiry_and_budget(cfg):
    app = create_app(cfg)
    p = app.state.playback
    p.limit = 1024
    (p.root / "old.mp3").write_bytes(b"x" * 512)
    p.entries["old"] = {"state": "ready", "used": time.time() - p.ttl - 1}
    p.clean()
    assert not p.entries and not list(p.root.iterdir())
    (p.root / "recent.mp3").write_bytes(b"x" * 1024)
    p.entries["recent"] = {"state": "ready", "used": time.time()}
    with pytest.raises(Exception) as exc:
        p.clean(128)
    assert exc.value.status_code == 429
    p.close()
    app.state.sources.close()


def test_preview_download_caps_and_redirect_validation(cfg):
    app = create_app(cfg)
    s = app.state.sources
    s.client.close()
    s.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 100)))
    with pytest.raises(SourceError, match="大小限制"):
        s.download("https://music.163.com/test", cfg.data / "test.part", max_bytes=32)
    s.client.close()
    s.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}))
    )
    with pytest.raises(SourceError):
        s.download("https://music.163.com/test", cfg.data / "test.part", max_bytes=MIB)
    s.close()
    app.state.playback.close()
