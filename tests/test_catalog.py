import json
import shutil
import subprocess

from app import media
from app.db import Database, dumps
from app.engine import Engine
from test_service import FakeSources, track

pytest_plugins = ["test_service"]


def test_folder_scan_local_unmatched_and_ape(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    folder = cfg.music / "本地收藏"
    folder.mkdir()
    shutil.copyfile(tone, folder / "无平台.flac")
    e.scan()
    p = e.db.one("SELECT * FROM playlists WHERE platform='local'")
    assert p["folder"] == "本地收藏"
    items = e.playlist_tracks(p)
    assert len(items) == 1 and items[0]["asset_id"] and items[0]["available"]
    assert ".ape" in media.EXTENSIONS
    assert (folder / "歌单.m3u8").read_text().splitlines()[-1] == "无平台.flac"


def test_two_playlist_download_once_flat_copies(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    t = track()
    e.db.save_track(t)
    p1 = e.local_playlist("第一张")
    p2 = e.local_playlist("第二张")
    jid = e.queue_track(t, p1)
    assert e.queue_track(t, p2) == jid
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    j = e.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert j["state"] == "succeeded", j
    assets = e.db.rows("SELECT * FROM assets")
    assert len(assets) == 2
    assert {a["path"].split("/")[0] for a in assets} == {"第一张", "第二张"}
    assert all(len(a["path"].split("/")) == 2 for a in assets)
    assert len(e.sources.calls) == 1
    assert len({media.sha(cfg.music / a["path"]) for a in assets}) == 1
    # another playlist reuses the completed file without another source download
    p3 = e.local_playlist("第三张")
    jid = e.queue_track(t, p3)
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    assert len(e.sources.calls) == 1 and len(e.folder_assets("第三张")) == 1


def test_enrich_ambiguous_does_not_touch_original(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    f = cfg.music / "未知.flac"
    shutil.copyfile(tone, f)
    before = media.sha(f)
    e.scan()
    a = e.db.one("SELECT * FROM assets")
    data = json.loads(a["data"])
    data.update(title="回声 (Live)", artists=["样本歌手"])
    e.db.execute("UPDATE assets SET data=? WHERE id=?", (dumps(data), a["id"]))
    e.sources.search = lambda *args: [track(), track("netease:102")]
    jid = e.enqueue("enrich", {"asset_id": a["id"]}, "补齐")
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    j = e.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert j["state"] == "review" and json.loads(j["result"])["needs_review"]
    assert media.sha(f) == before


def test_wav_tags_preserve_audio(cfg, tone):
    f = cfg.music / "样本.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(tone), str(f)], check=True)
    before = media.audio_hash(f)
    media.write_tags(f, track(), lyric="[00:00.00]一\n[00:03.00]二\n[00:06.00]三")
    tags, _, _ = media.read_tags(f)
    assert tags["title"] == track()["title"] and tags["has_lyrics"]
    assert media.audio_hash(f) == before


def test_unique_folder_binding_and_read_only_scan(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    for ident in ["1", "2"]:
        e.add_playlist({"platform": "netease", "source_id": ident, "name": "同名歌单", "url": "", "tracks": []})
    rows = e.db.rows("SELECT folder FROM playlists")
    assert len({p["folder"] for p in rows}) == 2
    assert all("/" not in p["folder"] for p in rows)


def test_scan_preserves_confirmed_id_without_embedded_tags(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    f = cfg.music / "原曲.flac"
    shutil.copyfile(tone, f)
    e.scan()
    a = e.db.one("SELECT * FROM assets")
    e.db.save_track(track())
    e.db.execute("UPDATE assets SET track_key=?,mtime_ns=0 WHERE id=?", ("netease:101", a["id"]))
    e.scan()
    saved = e.db.one("SELECT * FROM assets")
    assert saved["id"] == a["id"] and saved["track_key"] == "netease:101"


def test_long_chinese_filename_has_safe_bytes():
    assert len(media.safe_name("长" * 200).encode()) <= 200


def test_only_missing_playlist_member_is_materialized(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    t = track()
    e.db.save_track(t)
    p1 = e.local_playlist("原歌单")
    p2 = e.local_playlist("新歌单")
    jid = e.queue_track(t, p1)
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    assert not e.queue_missing([t], p1)
    assert len(e.queue_missing([t], p2)) == 1
    assert len(e.queue_missing([t], p2)) == 1
    assert e.db.one("SELECT count(*) n FROM jobs WHERE state='queued'")["n"] == 1


def test_real_migration_script_preserves_id_and_copies_membership(cfg, tone, tmp_path):
    from pathlib import Path

    e = Engine(cfg, Database(cfg.data / "music.sqlite3"), FakeSources(tone))
    folder = cfg.music / "示例用户喜欢的音乐"
    folder.mkdir()
    f = folder / "旧名.flac"
    shutil.copyfile(tone, f)
    st = f.stat()
    tags = media.read_tags(f)[0]
    t = track()
    e.db.save_track(t)
    e.db.execute(
        "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
        ("stableasset", "旧名.flac", t["key"], dumps(tags), st.st_size, st.st_mtime_ns, None, 1, 1),
    )
    for ident, name in [("10001", "原歌单"), ("10002", "示例账号喜欢的音乐")]:
        e.add_playlist({"platform": "netease", "source_id": ident, "name": name, "url": "", "tracks": [t]})
    e.db.set("queue_paused", True)
    row = {
        "path": "示例用户喜欢的音乐/旧名.flac",
        "new_path": "示例用户喜欢的音乐/样本.flac",
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "inode": st.st_ino,
        "track_key": t["key"],
        "suggested_tags": tags,
    }
    plan = {"audio": [row], "auxiliary": [], "pathmap": {row["path"]: row["new_path"]}, "summary": {"errors": []}}
    p = tmp_path / "plan.json"
    p.write_text(dumps(plan))
    script = (
        Path("tests/fixtures/legacy_folder_migration.py")
        .read_text()
        .replace("Path('/music')", f"Path({str(cfg.music)!r})")
        .replace("Path('/data')", f"Path({str(cfg.data)!r})")
        .replace("Path('/plan/迁移清单.json')", f"Path({str(p)!r})")
    )
    exec(compile(script, "migration-test", "exec"), {"__name__": "__main__"})
    a = e.db.one("SELECT * FROM assets WHERE id='stableasset'")
    assert a["path"] == row["new_path"] and (cfg.music / a["path"]).stat().st_ino == st.st_ino
    assert len(e.folder_assets("示例账号喜欢的音乐")) == 1
    assert media.audio_hash(cfg.music / a["path"]) == media.audio_hash(tone)


def test_target_added_during_distribution_is_not_lost(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    t = track()
    e.db.save_track(t)
    p1 = e.local_playlist("先加入")
    p2 = e.local_playlist("后加入")
    jid = e.queue_track(t, p1)
    original = e.copy_to_playlist
    injected = []

    def copy(asset, pid):
        result = original(asset, pid)
        if not injected:
            injected.append(True)
            assert e.queue_track(t, p2) == jid
        return result

    e.copy_to_playlist = copy
    e.run_job(e.db.one("SELECT * FROM jobs WHERE id=?", (jid,)))
    j = e.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert j["state"] == "succeeded"
    assert set(json.loads(j["result"])["delivered_to"]) == {p1, p2}
    assert len(e.folder_assets("后加入")) == 1 and len(e.sources.calls) == 1


def test_batch_enrichment_deduplicates_pending(cfg, tone):
    e = Engine(cfg, Database(cfg.data / "db"), FakeSources(tone))
    shutil.copyfile(tone, cfg.music / "未知.flac")
    e.scan()
    assets = e.db.rows("SELECT * FROM assets")
    first = e.queue_enrichment(assets)
    assert first and e.queue_enrichment(assets) == first


def test_atomic_playlist_concurrent_publish(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / '歌单.m3u8'
    contents = [('#EXTM3U\n' + str(i) * 2000).encode() for i in range(8)]
    barrier = Barrier(8)
    replace = media.os.replace

    def simultaneous_replace(source, target):
        barrier.wait(timeout=5)
        replace(source, target)

    monkeypatch.setattr(media.os, 'replace', simultaneous_replace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda value: media.atomic_bytes(path, value), contents))
    assert path.read_bytes() in contents
    assert list(tmp_path.iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o644


def test_atomic_playlist_failure_preserves_original(tmp_path, monkeypatch):
    import pytest

    path = tmp_path / '歌单.m3u8'
    path.write_bytes(b'original')

    def fail(source, target):
        raise OSError('simulated publication failure')

    monkeypatch.setattr(media.os, 'replace', fail)
    with pytest.raises(OSError):
        media.atomic_bytes(path, b'replacement')
    assert path.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [path]
