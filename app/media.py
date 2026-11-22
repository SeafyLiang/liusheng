import hashlib
import io
import json
import os
import re
import subprocess
import threading
from contextlib import nullcontext
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, USLT
from mutagen.mp4 import MP4, MP4Cover
from mutagen.monkeysaudio import MonkeysAudio
from PIL import Image

EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".ape"}
STAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


@lru_cache(maxsize=256)
def _lyrics_version(path, version, sidecar_version):
    return read_tags(Path(path))[2]


def cached_lyrics(path):
    st = path.stat()
    sidecar = path.with_suffix('.lrc')
    lrc = sidecar.stat() if sidecar.is_file() else None
    return _lyrics_version(str(path.resolve()), (st.st_mtime_ns, st.st_size),
                           (lrc.st_mtime_ns, lrc.st_size) if lrc else None)


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


TRANSCODE_LOCK = threading.RLock()


def command(args, timeout=240):
    with TRANSCODE_LOCK if Path(args[0]).name == "ffmpeg" else nullcontext():
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    if p.returncode:
        raise ValueError("音频检查失败：" + p.stderr.decode("utf8", "replace")[-250:])
    return p.stdout


def probe(path, expected=0, decode=True):
    data = json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bits_per_raw_sample:format=duration,bit_rate",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    if not data.get("streams"):
        raise ValueError("没有可识别的音轨")
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError("无有效播放时长")
    if expected and abs(duration - expected) > max(3, expected * 0.02):
        raise ValueError(f"时长不符，可能为试听或错误版本：文件 {duration:.1f} 秒 / 目标 {expected:.1f} 秒")
    if decode:
        command(
            ["ffmpeg", "-nostdin", "-threads", "1", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"],
            timeout=max(240, duration),
        )
    return {**data["streams"][0], "duration": duration, "bitrate": int(data["format"].get("bit_rate") or 0)}


def audio_hash(path):
    # APE 的末包可能包含尾部标签字节；使用完整解码 PCM 校验，避免标签更新造成假阳性。
    return (
        command(
            [
                "ffmpeg",
                "-nostdin",
                "-threads",
                "1",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                "pcm_s32le" if path.suffix.lower() == ".ape" else "copy",
                "-f",
                "hash",
                "-hash",
                "sha256",
                "-",
            ]
        )
        .decode()
        .strip()
    )


def safe_name(text):
    return re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", text).strip(" .").encode("utf-8")[:200].decode("utf-8", "ignore") or "未命名"


def atomic_bytes(path, data):
    # 不同请求可能同时刷新同一歌单，临时文件必须由各自写入者独占。
    import tempfile

    tmp = None
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.musicprep-', suffix='.pending', delete=False) as f:
            tmp = Path(f.name)
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def parse_lrc(raw):
    rows = []
    off = re.search(r"\[offset:([+-]?\d+)\]", raw, re.IGNORECASE)
    offset = int(off[1]) / 1000 if off else 0
    for line in raw.splitlines():
        body = STAMP.sub("", line).strip()
        for m in STAMP.finditer(line):
            if int(m[2]) >= 60:
                raise ValueError("歌词秒数格式异常")
            sec = int(m[1]) * 60 + int(m[2]) + float("0." + (m[3] or "0")) + offset
            if body:
                rows.append((round(sec, 3), body))
    return sorted(set(rows), key=lambda x: x[0])


def build_lrc(raw, duration):
    if raw.get("nolyric"):
        return None, {"status": "instrumental", "label": "来源标记纯音乐"}
    rows = parse_lrc(raw.get("lrc", {}).get("lyric", ""))
    if len(rows) < 3:
        return None, {"status": "missing", "label": "未找到同步歌词"}
    if rows[0][0] < 0 or rows[-1][0] > duration + 3:
        return None, {"status": "review", "label": "歌词时间轴待确认"}
    trans = parse_lrc(raw.get("tlyric", {}).get("lyric", ""))
    output = []
    translated = 0
    for sec, body in rows:
        match = [text for t, text in trans if abs(t - sec) <= 0.06 and text != body]
        if match:
            body += " / " + " / ".join(match)
            translated += 1
        cent = round(sec * 100)
        output.append(f"[{cent // 6000:02d}:{(cent % 6000) // 100:02d}.{cent % 100:02d}]" + body)
    return "\n".join(output) + "\n", {"status": "synced", "label": "同步歌词 · 待听校", "translation_lines": translated, "lines": len(rows)}


def cover_jpeg(data):
    if len(data) > 12 * 1024 * 1024:
        raise ValueError("封面超过大小限制")
    with Image.open(io.BytesIO(data)) as im:
        if im.width * im.height > 24_000_000:
            raise ValueError("封面像素过多")
        im.load()
        im = im.convert("RGB")
        im.thumbnail((800, 800))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()


def read_tags(path):
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("音频格式无法识别")
    tags = audio.tags or {}

    def values(*keys):
        for k in keys:
            try:
                v = tags.get(k)
            except (ValueError, KeyError):
                continue
            if v:
                if isinstance(audio, MonkeysAudio):
                    return str(v).split('\x00')
                return [str(x) for x in getattr(v, "text", v)]
        return []

    title = values("title", "TIT2", "\xa9nam")
    artists = values("artist", "TPE1", "\xa9ART")
    album = values("album", "TALB", "\xa9alb")
    embedded = values("lyrics", "\xa9lyr")
    if hasattr(tags, "getall"):
        uslt = tags.getall("USLT")
        embedded = [uslt[0].text] if uslt else embedded
    cover = None
    if getattr(audio, "pictures", None):
        cover = audio.pictures[0].data
    elif hasattr(tags, "getall") and tags.getall("APIC"):
        cover = tags.getall("APIC")[0].data
    elif tags.get("covr"):
        cover = bytes(tags["covr"][0])
    if isinstance(audio, MonkeysAudio) and tags.get('Cover Art (Front)'):
        cover = bytes(tags['Cover Art (Front)']).split(b'\x00', 1)[-1]
    side = path.with_suffix(".lrc")
    lrc = (
        side.read_text(encoding="utf-8-sig", errors="replace")
        if side.is_file() and not side.is_symlink() and side.stat().st_size < 1024 * 1024
        else ""
    )
    lyric = lrc or (embedded[0] if embedded else "")
    try:
        synced = len(parse_lrc(lyric)) >= 3
    except ValueError:
        synced = False
    track_key = (values("source_track_id", "TXXX:SOURCE_TRACK_ID") or [""])[0]
    if isinstance(audio, MP4) and not track_key:
        stored = tags.get("----:com.apple.iTunes:SOURCE_TRACK_ID") or []
        track_key = bytes(stored[0]).decode("utf8", "replace") if stored else ""
    netease = values("netease_id", "TXXX:NETEASE_ID")
    if not track_key and netease:
        track_key = f"netease:{netease[0]}"
    missing = [
        name for name, exists in [("标题", title), ("歌手", artists), ("专辑", album), ("封面", cover), ("歌词", lyric)] if not exists
    ]
    return (
        {
            "title": title[0] if title else "",
            "artists": artists,
            "album": album[0] if album else "",
            "has_cover": bool(cover),
            "has_lyrics": bool(lyric),
            "lyrics_synced": synced,
            "missing": missing,
            "track_key": track_key,
            "duration": float(getattr(audio.info, "length", 0)),
            "format": path.suffix.lstrip(".").upper(),
        },
        cover,
        lyric,
    )


def write_tags(path, track, lyric=None, cover=None, only_missing=True, mode="all"):
    before, _, _ = read_tags(path)
    audio = mutagen.File(path)
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    artists = track.get("artists") or []
    values = {
        "title": track.get("title", ""),
        "artist": artists,
        "album": track.get("album", ""),
        "albumartist": track.get("album_artists") or artists,
        "tracknumber": str(track.get("track") or ""),
        "discnumber": str(track.get("disc") or ""),
        "source_track_id": track["key"],
    }
    if track.get("publish_time", 0) > 0:
        values["date"] = datetime.fromtimestamp(track["publish_time"] / 1000, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    if track["platform"] == "netease":
        values["netease_id"] = track["id"]
    if mode != "all":
        values = {"source_track_id": track["key"]}
    if only_missing:
        if before["has_lyrics"]:
            lyric = None
        if before["has_cover"]:
            cover = None
    if isinstance(audio, FLAC) or path.suffix.lower() in (".ogg", ".opus"):
        for k, v in values.items():
            if v and (not only_missing or not t.get(k)):
                t[k] = v if isinstance(v, list) else [v]
        if lyric:
            t["lyrics"] = [lyric]
        if cover and isinstance(audio, FLAC):
            picture = Picture()
            picture.type, picture.mime, picture.desc, picture.data = 3, "image/jpeg", "Cover", cover
            if not only_missing:
                audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()
    elif isinstance(audio, MP4):
        keys = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART", "date": "\xa9day"}
        for k, atom in keys.items():
            v = values.get(k)
            if v and (not only_missing or not t.get(atom)):
                t[atom] = v if isinstance(v, list) else [v]
        t["----:com.apple.iTunes:SOURCE_TRACK_ID"] = [track["key"].encode()]
        if lyric:
            t["\xa9lyr"] = [lyric]
        if cover:
            t["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    elif isinstance(audio, MonkeysAudio):
        for k, v in values.items():
            if v and (not only_missing or not t.get(k)):
                t[k] = '\x00'.join(v) if isinstance(v, list) else v
        if lyric:
            t['lyrics'] = lyric
        if cover:
            from mutagen.apev2 import APEBinaryValue
            t['Cover Art (Front)'] = APEBinaryValue(b'cover.jpg\x00' + cover)
        audio.save()
    elif path.suffix.lower() in (".mp3", ".wav"):

        frames = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2, "tracknumber": TRCK, "discnumber": TPOS, "date": TDRC}
        for k, frame in frames.items():
            v = values.get(k)
            if v and (not only_missing or not t.get(frame.__name__)):
                t.add(frame(encoding=1, text=[" / ".join(v) if isinstance(v, list) else v]))
        t.add(TXXX(encoding=1, desc="SOURCE_TRACK_ID", text=[track["key"]]))
        if track["platform"] == "netease":
            t.add(TXXX(encoding=1, desc="NETEASE_ID", text=[track["id"]]))
        if lyric:
            t.delall("USLT")
            t.add(USLT(encoding=1, lang="und", desc="", text=lyric))
        if cover:
            t.delall("APIC")
            t.add(APIC(encoding=1, mime="image/jpeg", type=3, desc="Cover", data=cover))
        if path.suffix.lower() == '.mp3':
            audio.save(v2_version=3, v23_sep=" / ")
        else:
            audio.save()
    else:
        raise ValueError("此格式仅支持扫描；请投递 FLAC、MP3 或 M4A 整理")
    return read_tags(path)[0]
