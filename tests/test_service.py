import json
import os
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import Config, within
from app.db import Database, dumps, now
from app.engine import Engine, snapshot_diff
from app.main import create_app
from app.sources import Sources, SourceError, validate_url, exact_candidate
from app import media


def track(key="netease:101", title="回声 (Live)", duration=10):
    platform, ident = key.split(":", 1)
    return {
        "key": key,
        "id": ident,
        "platform": platform,
        "title": title,
        "artists": ["样本歌手"],
        "album": "验证专辑",
        "duration": duration,
        "track": 2,
    }


@pytest.fixture
def cfg(tmp_path):
    c = Config(tmp_path / "data", tmp_path / "music", tmp_path / "inbox", tmp_path / "static", False)
    c.prepare()
    return c


@pytest.fixture(scope="session")
def tone(tmp_path_factory):
    path = tmp_path_factory.mktemp("audio") / "sample.flac"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-ac", "2", str(path)], check=True
    )
    return path


class FakeSources:
    def __init__(self, tone):
        self.tone, self.calls = tone, []
        self.bad_first = False

    def details(self, ids):
        return [track(f"netease:{ident}") for ident in ids]

    def audio_url(self, t, provider):
        self.calls.append(provider)
        if self.bad_first and provider == "bad":
            raise SourceError("来源不可达", "unavailable")
        return {"url": "https://music.163.com/test"}

    def download(self, url, dest, cancel):
        cancel()
        shutil.copyfile(self.tone, dest)

    def lyrics(self, t):
        return {"lrc": {"lyric": "[00:00.00]第一句\n[00:03.00]第二句\n[00:06.00]第三句"}, "tlyric": {"lyric": "[00:03.00]Translation"}}


@pytest.fixture
def engine(cfg, tone):
    db = Database(cfg.data / "test.sqlite")
    return Engine(cfg, db, FakeSources(tone))


def run(e, kind, payload, key=None):
    jid = e.enqueue(kind, payload, "测试任务", key)
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    return e.db.one("SELECT * FROM jobs WHERE id=?", (jid,))


def test_login_csrf_logout(cfg):
    app = create_app(cfg)
    with TestClient(app) as c:
        assert c.get("/api/library").status_code == 401
        assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
        pw = (cfg.data / "initial-password.txt").read_text().strip()
        r = c.post("/api/login", json={"password": pw})
        assert r.status_code == 200 and "HttpOnly" in r.headers["set-cookie"]
        assert c.get("/api/library").status_code == 200
        assert c.post("/api/library/scan").status_code == 403
        csrf = {"X-CSRF-Token": r.json()["csrf"]}
        assert c.post("/api/library/scan", headers={**csrf, "Origin": "http://evil.test"}).status_code == 403
        assert c.post("/api/library/scan", headers=csrf).status_code == 200
        assert c.post("/api/logout", headers=csrf).status_code == 200
        assert c.get("/api/jobs").status_code == 401


def test_password_change_invalidates_sessions(cfg):
    app = create_app(cfg)
    with TestClient(app) as c:
        pw = (cfg.data / "initial-password.txt").read_text().strip()
        r = c.post("/api/login", json={"password": pw})
        csrf = {"X-CSRF-Token": r.json()["csrf"]}
        assert c.post("/api/password", headers=csrf, json={"current": pw, "password": "new-test-password-123"}).status_code == 200
        assert c.get("/api/library").status_code == 401
        assert not (cfg.data / "initial-password.txt").exists()


@pytest.mark.parametrize("peer,trusted", [("10.20.88.1", True), ("192.0.2.5", False)])
def test_https_proxy_origin_and_cookie(cfg, peer, trusted):
    app = create_app(cfg)
    proxy = ProxyHeadersMiddleware(app, trusted_hosts=["10.20.88.1"])
    origin = "https://music.example.com"
    with TestClient(proxy, base_url="http://music.example.com", client=(peer, 1234)) as client:
        password = (cfg.data / "initial-password.txt").read_text().strip()
        headers = {"Origin": origin, "X-Forwarded-Proto": "https", "Sec-Fetch-Site": "same-origin"}
        response = client.post("/api/login", json={"password": password}, headers=headers)
        if not trusted:
            assert response.status_code == 403
            assert "set-cookie" not in response.headers
            return
        assert response.status_code == 200
        assert "; Secure" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        csrf = {**headers, "X-CSRF-Token": response.json()["csrf"]}
        assert client.patch(origin + "/api/settings", json={}, headers=csrf).status_code == 200
        assert client.patch(origin + "/api/settings", json={}, headers=headers).status_code == 403
        assert client.patch(origin + "/api/settings", json={}, headers={**csrf, "Origin": "https://evil.test"}).status_code == 403


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://169.254.169.254/x",
        "file:///etc/passwd",
        "https://music.163.com.evil.test/a",
        "https://user:pass@music.163.com/a",
        "https://music.163.com:2209/a",
    ],
)
def test_external_urls_restricted(url):
    with pytest.raises(SourceError):
        validate_url(url, media=True)


def test_paths_and_symlinks(cfg, tmp_path):
    (cfg.music / "escape").symlink_to(tmp_path)
    for value in ("../outside", "/etc/passwd", "escape/anything"):
        with pytest.raises(ValueError):
            within(cfg.music, value)


def test_version_matching_and_duration():
    a = track()
    assert exact_candidate(a, track())
    assert not exact_candidate(a, track(title="回声"))
    assert not exact_candidate(a, track(duration=30))
    assert not exact_candidate(a, {**track(), "artists": ["另一歌手"]})


def test_snapshot_diff_member_order():
    a, b, c, d = (track(f"netease:{i}") for i in [1, 2, 3, 4])
    diff = snapshot_diff([a, b, c], [c, a, d])
    assert diff["added"] == [d] and diff["removed"] == [b] and diff["reordered"]
    assert not snapshot_diff([a, b], [a, b, c])["reordered"]


def test_complete_snapshot_rejects_truncated(cfg):
    s = Sources(Database(cfg.data / "sources.sqlite"))
    s.get = lambda *args: {"code": 200, "playlist": {"trackCount": 3, "trackIds": [{"id": 1}], "tracks": []}}
    with pytest.raises(SourceError, match="不完整"):
        s.playlist("netease", "123")
    s.close()


def test_metadata_batch_fills_all_ids(cfg):
    s = Sources(Database(cfg.data / "sources.sqlite"))
    s.get = lambda *args: {
        "code": 200,
        "playlist": {
            "name": "测试",
            "trackCount": 2,
            "trackIds": [{"id": 1}, {"id": 2}],
            "tracks": [{"id": 1, "name": "一", "artists": [{"name": "甲"}], "duration": 10000}],
        },
    }
    s.details = lambda ids: [track(f"netease:{i}") for i in ids]
    result = s.playlist("netease", "123")
    assert [t["id"] for t in result["tracks"]] == ["1", "2"]
    s.close()


def test_qq_rejects_first_page(cfg):
    s = Sources(Database(cfg.data / "sources.sqlite"))
    s.get = lambda *args: {"code": 0, "cdlist": [{"total_song_num": 500, "songlist": [{"songmid": "a"}]}]}
    with pytest.raises(SourceError, match="全部"):
        s.playlist("tencent", "123")
    s.close()


def test_redirect_validation(cfg):
    s = Sources(Database(cfg.data / "sources.sqlite"))
    s.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})))
    with pytest.raises(SourceError):
        s.request("https://163cn.tv/abc")
    s.close()


def test_failed_sync_keeps_snapshot_and_audio(engine, tone):
    a, b = track(), track("netease:102")
    snapshot = {"platform": "netease", "source_id": "1", "name": "歌单", "url": "https://music.163.com/playlist?id=1", "tracks": [a, b]}
    pid = engine.add_playlist(snapshot)["id"]
    source = engine.config.music / "existing.flac"
    shutil.copyfile(tone, source)
    original = media.sha(source)

    def broken(*args):
        raise SourceError("分页缺失", "incomplete")

    engine.sources.playlist = broken
    j = run(engine, "sync", {"playlist_id": pid})
    assert j["state"] == "failed"
    row = engine.db.one("SELECT * FROM playlists WHERE id=?", (pid,))
    assert json.loads(row["snapshot"]) == [a, b]
    assert media.sha(source) == original


def test_playlist_removal_only_unlinks(engine, tone):
    a, b = track(), track("netease:102")
    snapshot = {"platform": "netease", "source_id": "1", "name": "歌单", "url": "https://music.163.com/playlist?id=1", "tracks": [a, b]}
    pid = engine.add_playlist(snapshot)["id"]
    source = engine.config.music / "keep.flac"
    shutil.copyfile(tone, source)
    engine.sources.playlist = lambda *args: {**snapshot, "tracks": [b]}
    j = run(engine, "sync", {"playlist_id": pid})
    assert j["state"] == "succeeded"
    assert json.loads(j["result"])["removed"] == 1
    assert source.exists()


def test_job_dedup_and_schedule(engine):
    first = engine.enqueue("acquire", {"key": "netease:101"}, "a")
    assert engine.enqueue("acquire", {"key": "netease:101"}, "b") == first
    snapshot = {"platform": "netease", "source_id": "1", "name": "歌单", "url": "https://music.163.com/playlist?id=1", "tracks": [track()]}
    pid = engine.add_playlist(snapshot)["id"]
    engine.db.execute("UPDATE playlists SET next_check=?", (now() - 10,))
    engine.schedule()
    engine.schedule()
    assert len(engine.db.rows("SELECT * FROM jobs WHERE kind='sync'")) == 1
    assert engine.db.one("SELECT payload FROM jobs WHERE kind='sync'")["payload"] == dumps({"playlist_id": pid, "weekly_due": False})


def test_download_pipeline_source_priority_and_reuse(engine, tone):
    t = track()
    engine.db.save_track(t)
    engine.db.set("providers", ["good", "not-called"])
    j = run(engine, "acquire", {"key": t["key"]})
    assert j["state"] == "succeeded", j["error"]
    assert engine.sources.calls == ["good"]
    asset = engine.asset_for(t["key"])
    path = engine.config.music / asset["path"]
    tags, _, lyric = media.read_tags(path)
    assert tags["title"] == t["title"] and tags["has_lyrics"]
    assert "Translation" in lyric
    assert media.audio_hash(path) == media.audio_hash(tone)
    assert path.with_suffix(".lrc").exists()
    j2 = run(engine, "acquire", {"key": t["key"]})
    assert j2["state"] == "succeeded" and json.loads(j2["result"])["reused"]
    assert engine.sources.calls == ["good"]


def test_fallback_logs_unavailable_not_notfound(engine):
    engine.db.save_track(track())
    engine.db.set("providers", ["bad", "good"])
    engine.sources.bad_first = True
    j = run(engine, "acquire", {"key": track()["key"]})
    assert j["state"] == "succeeded"
    assert engine.sources.calls == ["bad", "good"]
    assert engine.db.rows("SELECT status FROM attempts WHERE job_id=? ORDER BY id", (j["id"],))[0]["status"] == "unavailable"


def test_preview_and_corrupt_rejected(tone, tmp_path):
    with pytest.raises(ValueError, match="试听"):
        media.probe(tone, 200)
    bad = tmp_path / "bad.mp3"
    bad.write_text("<html>login</html>")
    with pytest.raises(ValueError):
        media.probe(bad, 100)


def test_lyrics_translation_and_boundaries():
    s, state = media.build_lrc({"lrc": {"lyric": "[00:00]A\n[00:02]B\n[00:04]C"}, "tlyric": {"lyric": "[00:02.03]乙"}}, 5)
    assert "B / 乙" in s and state["translation_lines"] == 1
    assert media.build_lrc({"nolyric": True}, 5)[1]["status"] == "instrumental"
    assert media.build_lrc({"lrc": {"lyric": "[00:00]A\n[00:02]B\n[00:40]C"}}, 5)[1]["status"] == "review"


def test_scan_readonly_unicode_and_mapping(engine, tone):
    name = "Sofi\u0301a.flac"
    p = engine.config.music / name
    shutil.copyfile(tone, p)
    import unicodedata

    seed = {"files": {unicodedata.normalize("NFC", name): {"track": track(), "size": p.stat().st_size, "source": "历史记录"}}}
    (engine.config.data / "bootstrap.json").write_text(dumps(seed))
    before = media.sha(p)
    r = engine.scan()
    assert r["scanned"] == 1 and r["written_audio"] == 0
    a = engine.asset_for(track()["key"])
    assert a and a["path"] == name and media.sha(p) == before


def test_lock_prevents_enrichment(engine, tone):
    engine.db.save_track(track())
    j = run(engine, "acquire", {"key": track()["key"]})
    assert j["state"] == "succeeded"
    a = engine.asset_for(track()["key"])
    d = json.loads(a["data"])
    d["locked"] = True
    engine.db.execute("UPDATE assets SET data=? WHERE id=?", (dumps(d), a["id"]))
    before = media.sha(engine.config.music / a["path"])
    j = run(engine, "enrich", {"asset_id": a["id"]})
    assert j["state"] == "failed" and "锁定" in j["error"]
    assert media.sha(engine.config.music / a["path"]) == before


def test_publish_recovery_after_audio_replace(engine, tone, monkeypatch):
    engine.db.save_track(track())
    original = engine.publish

    def interrupt(jid, p, stage):
        dest = engine.config.music / p["path"]
        dest.parent.mkdir(parents=True)
        shutil.copyfile(stage / p["audio"], dest)
        raise RuntimeError("simulated crash after audio publish")

    monkeypatch.setattr(engine, "publish", interrupt)
    j = run(engine, "acquire", {"key": track()["key"]})
    assert j["state"] == "failed"
    engine.db.execute("UPDATE jobs SET state='queued' WHERE id=?", (j["id"],))
    monkeypatch.setattr(engine, "publish", original)
    engine.run_job(engine.db.one("SELECT * FROM jobs WHERE id=?", (j["id"],)))
    assert engine.db.one("SELECT state FROM jobs WHERE id=?", (j["id"],))["state"] == "succeeded"
    assert engine.sources.calls == ["gd"]
    a = engine.asset_for(track()["key"])
    assert (engine.config.music / a["path"]).with_suffix(".lrc").exists()


def test_user_file_change_blocks_publish(engine, tone, monkeypatch):
    engine.db.save_track(track())
    original = engine.publish

    def conflict(jid, p, stage):
        dest = engine.config.music / p["path"]
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"user file, must not overwrite")
        return original(jid, p, stage)

    monkeypatch.setattr(engine, "publish", conflict)
    j = run(engine, "acquire", {"key": track()["key"]})
    assert j["state"] == "failed" and "外部变更" in j["error"]
    assert next(engine.config.music.rglob("*.flac")).read_bytes() == b"user file, must not overwrite"


def test_mapping_reuses_same_asset(engine):
    engine.db.save_track(track())
    assert run(engine, "acquire", {"key": track()["key"]})["state"] == "succeeded"
    other = track("tencent:another")
    engine.db.save_track(other)
    engine.db.execute("INSERT INTO mappings VALUES (?,?,?)", (other["key"], track()["key"], now()))
    assert engine.asset_for(other["key"])["id"] == engine.asset_for(track()["key"])["id"]


def test_external_lrc_change_detected_by_scan(engine, tone):
    p = engine.config.music / "outside.flac"
    shutil.copyfile(tone, p)
    media.write_tags(p, track())
    engine.db.save_track(track())
    engine.scan()
    assert not json.loads(engine.asset_for(track()["key"])["data"])["has_lyrics"]
    p.with_suffix(".lrc").write_text("[00:00]A\n[00:02]B\n[00:04]C")
    engine.scan()
    assert json.loads(engine.asset_for(track()["key"])["data"])["has_lyrics"]


@pytest.mark.parametrize("extension,codec", [("mp3", "libmp3lame"), ("m4a", "aac")])
def test_other_formats_keep_audio_and_identity(tmp_path, tone, extension, codec):
    p = tmp_path / ("encoded." + extension)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(tone), "-c:a", codec, str(p)], check=True)
    before = media.audio_hash(p)
    media.write_tags(p, track(), lyric="[00:00]A\n[00:02]B\n[00:04]C")
    tags, _, lyric = media.read_tags(p)
    assert tags["track_key"] == track()["key"] and tags["has_lyrics"]
    assert media.audio_hash(p) == before
    media.write_tags(p, track(title="不应覆盖"), only_missing=True)
    assert media.read_tags(p)[0]["title"] == track()["title"]


def test_cancel_before_work_and_cleanup(engine):
    engine.db.save_track(track())
    jid = engine.enqueue("acquire", {"key": track()["key"]}, "取消测试")
    stage = engine.config.data / "staging" / jid
    stage.mkdir()
    (stage / "partial.part").write_bytes(b"partial")
    engine.db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))
    engine.run_job(engine.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    assert engine.db.one("SELECT state FROM jobs WHERE id=?", (jid,))["state"] == "cancelled"
    assert engine.sources.calls == [] and not stage.exists()


def test_inbox_upload_import_and_reuse(cfg, tone):
    app = create_app(cfg)
    engine = app.state.engine
    engine.sources = FakeSources(tone)
    engine.db.save_track(track())
    with TestClient(app) as client:
        password = (cfg.data / "initial-password.txt").read_text().strip()
        response = client.post("/api/login", json={"password": password})
        client.headers["X-CSRF-Token"] = response.json()["csrf"]
        uploaded = client.post("/api/inbox/upload", files={"file": ("../演示.flac", tone.read_bytes(), "audio/flac")})
        assert uploaded.status_code == 200
        name = uploaded.json()["file"]
        source = cfg.inbox / name
        assert source.parent == cfg.inbox and source.is_file()
        assert client.post("/api/inbox/import", json={"file": name, "key": "netease:101"}).status_code == 400
        os.utime(source, (now() - 10, now() - 10))
        response = client.post("/api/inbox/import", json={"file": name, "key": "netease:101"})
        assert response.status_code == 200
        jid = response.json()["job_id"]
        engine.run_job(engine.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
        job = engine.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        assert job["state"] == "succeeded", job["error"]
        asset = engine.asset_for("netease:101")
        assert asset and media.audio_hash(cfg.music / asset["path"]) == media.audio_hash(tone)
        assert source.read_bytes() == tone.read_bytes()
        assert not engine.sources.calls
        assert client.post("/api/inbox/import", json={"file": name, "key": "netease:101"}).json() == {"existing": True}


def test_cancelled_scan_keeps_unseen_assets(engine, tone, monkeypatch):
    paths = [engine.config.music / f"{i:02}.flac" for i in range(25)]
    for path in paths:
        shutil.copyfile(tone, path)
    engine.scan()
    paths[-1].unlink()
    original_check = engine.check
    checks = 0

    def cancel_during_scan(jid):
        nonlocal checks
        checks += 1
        if checks == 6:
            engine.db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))
        original_check(jid)

    monkeypatch.setattr(engine, "check", cancel_during_scan)
    job = run(engine, "scan", {})
    assert job["state"] == "cancelled"
    assert engine.db.one("SELECT count(*) n FROM assets WHERE present=1")["n"] == 25
    engine.scan()
    assert engine.db.one("SELECT count(*) n FROM assets WHERE present=1")["n"] == 24


def test_scan_preserves_edits_made_during_batch(engine, tone, monkeypatch):
    path = engine.config.music / "sample.flac"
    shutil.copyfile(tone, path)
    media.write_tags(path, track())
    engine.db.save_track(track())
    engine.db.save_track(track("netease:202"))
    engine.scan()
    asset = engine.asset_for("netease:101")
    path.with_suffix(".lrc").write_text("[00:00]A\n[00:02]B\n[00:04]C")
    original_read = media.read_tags

    def read_while_user_locks(p):
        result = original_read(p)
        data = json.loads(engine.db.one("SELECT data FROM assets WHERE id=?", (asset["id"],))["data"])
        data["locked"] = True
        engine.db.execute("UPDATE assets SET data=?,track_key=? WHERE id=?", (dumps(data), "netease:202", asset["id"]))
        return result

    monkeypatch.setattr(media, "read_tags", read_while_user_locks)
    engine.scan()
    current = engine.db.one("SELECT * FROM assets WHERE id=?", (asset["id"],))
    assert current["track_key"] == "netease:202"
    assert json.loads(current["data"])["locked"] is True
