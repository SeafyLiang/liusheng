"""受限域名的来源适配器。平台和公共接口会变，失败不等同于未找到。"""

import base64
import html
import ipaddress
import json
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx

from .db import now

GD = "https://music-api.gdstudio.xyz/api.php"
META_HOSTS = ("music.163.com", "163cn.tv", "y.qq.com", "c.y.qq.com", "u.y.qq.com", "music-api.gdstudio.xyz", "lrclib.net", "www.kuwo.cn", "y.music.163.com", "music.apple.com", "open.spotify.com")
MEDIA_SUFFIXES = (
    "music.126.net",
    "music.163.com",
    "qq.com",
    "qqmusic.qq.com",
    "gtimg.cn",
    "kuwo.cn",
    "joox.com",
    "kugou.com",
    "music.gdstudio.xyz",
    "gdstudio.xyz",
    "mzstatic.com",
    "scdn.co",
)


class SourceError(Exception):
    def __init__(self, message, code="unavailable"):
        super().__init__(message)
        self.code = code


def validate_url(url, media=False):
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("https", "http") or u.username or u.password or u.port not in (None, 80, 443):
        raise SourceError("来源返回了不受支持的链接", "invalid_url")
    try:
        ipaddress.ip_address(host)
        raise SourceError("不接受 IP 形式的外部资源链接", "invalid_url")
    except ValueError:
        pass
    allowed = any(host == s or host.endswith("." + s) for s in MEDIA_SUFFIXES) if media else host in META_HOSTS
    if not allowed:
        raise SourceError("来源资源域名未接入，请人工检查", "unsupported_host")
    return url


def netease_track(s):
    al = s.get("al") or s.get("album") or {}
    ar = s.get("ar") or s.get("artists") or []
    aid = s.get("id")
    if not aid or not s.get("name"):
        raise SourceError("曲目详情不完整", "incomplete")
    return {
        "key": f"netease:{aid}",
        "platform": "netease",
        "id": str(aid),
        "title": s["name"],
        "artists": [a["name"] for a in ar if a.get("name")],
        "album": al.get("name", ""),
        "album_artists": [a["name"] for a in al.get("artists", []) if a.get("name")],
        "duration": (s.get("dt") or s.get("duration") or 0) / 1000,
        "cover": (al.get("picUrl") or "").replace("http://", "https://"),
        "track": s.get("no"),
        "disc": s.get("cd") or s.get("disc"),
        "publish_time": al.get("publishTime") or s.get("publishTime") or 0,
        "url": f"https://music.163.com/song?id={aid}",
    }


def qq_track(s):
    mid = s.get("songmid") or s.get("mid")
    album = s.get("album") or {}
    am = s.get("albummid") or album.get("mid")
    if not mid:
        raise SourceError("QQ 曲目详情缺少 mid", "incomplete")
    return {
        "key": f"tencent:{mid}",
        "platform": "tencent",
        "id": mid,
        "song_id": s.get("songid") or s.get("id"),
        "media_mid": (s.get("file") or {}).get("media_mid") or s.get("strMediaMid") or mid,
        "title": html.unescape(s.get("songname") or s.get("title") or s.get("name") or ""),
        "artists": [html.unescape(a["name"]) for a in s.get("singer", [])],
        "album": html.unescape(s.get("albumname") or album.get("title") or album.get("name") or ""),
        "duration": s.get("interval") or 0,
        "cover": f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{am}.jpg" if am else "",
        "url": f"https://y.qq.com/n/ryqq/songDetail/{mid}",
    }


def normalize(text):
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def exact_candidate(a, b):
    # 不移除括号；Live、翻唱等版本须一致。候选缺时长不能自动接受。
    return (
        normalize(a["title"]) == normalize(b["title"])
        and {normalize(x) for x in a["artists"]} == {normalize(x) for x in b["artists"]}
        and a.get("duration", 0) > 0
        and b.get("duration", 0) > 0
        and abs(a["duration"] - b["duration"]) <= 2
    )


class Sources:
    def __init__(self, db):
        self.db = db
        self.lx = None
        self.lock = threading.Lock()
        self.last = {}
        self.client = httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            follow_redirects=False,
        )

        proxy = os.getenv('SPOTIFY_HTTPS_PROXY')
        self.spotify_client = httpx.Client(proxy=proxy, timeout=httpx.Timeout(30, connect=10),
                                          headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=False) if proxy else None

    def close(self):
        self.client.close()
        if self.spotify_client:
            self.spotify_client.close()

    def status(self, source, status, detail="", delay=0):
        self.db.execute(
            "INSERT INTO source_health VALUES (?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET status=excluded.status,detail=excluded.detail,checked=excluded.checked,blocked_until=excluded.blocked_until",
            (source, status, detail[:300], now(), now() + delay if delay else 0),
        )

    def request(self, url, params=None, media=False, payload=None):
        validate_url(url, media)
        host = urlparse(url).hostname or ""
        source = "gd" if host == "music-api.gdstudio.xyz" else "tencent" if host.endswith("qq.com") else "lrclib" if host == "lrclib.net" else "kuwo" if host.endswith("kuwo.cn") else host if host in ("music.apple.com", "open.spotify.com") else "netease"
        wait = 6.5 if source == "gd" else 1.0 if source == "lrclib" else 0.5
        health = self.db.one("SELECT blocked_until FROM source_health WHERE source=?", (source,))
        if health and health["blocked_until"] > now():
            raise SourceError("来源暂时冷却，请稍后重试", "cooldown")
        with self.lock:
            time.sleep(max(0, wait - (time.monotonic() - self.last.get(source, 0))))
            self.last[source] = time.monotonic()
        try:
            for _ in range(5):
                headers = {"Referer": "https://y.qq.com/"} if source == "tencent" else {"User-Agent": "Liusheng/1.0.0 (personal music library)"} if source == "lrclib" else {}
                client = self.spotify_client if urlparse(url).hostname == 'open.spotify.com' and self.spotify_client else self.client
                response = client.post(url, params=params, json=payload, headers=headers) if payload is not None else client.get(url, params=params, headers=headers)
                if response.is_redirect:
                    url = str(response.next_request.url)
                    params = None
                    payload = None
                    validate_url(url, media)
                    continue
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After", "300")
                    delay = min(3600, max(30, int(retry))) if retry.isdigit() else 300
                    self.status(source, "limited", "来源限流，已暂停访问", delay)
                    raise SourceError("来源限流，已暂停访问", "rate_limited")
                if response.status_code in (401, 403):
                    self.status(source, "unavailable", f"HTTP {response.status_code}", 120)
                    raise SourceError("来源拒绝访问或需要登录", "auth_required")
                if response.status_code == 404 and source == "lrclib":
                    raise SourceError("LRCLIB 未找到匹配歌词", "not_found")
                response.raise_for_status()
                if len(response.content) > 20 * 1024 * 1024:
                    raise SourceError("来源响应过大", "invalid_response")
                self.status(source, "ok", "最近请求成功")
                return response
            raise SourceError("分享链接重定向次数过多", "invalid_url")
        except httpx.HTTPError as e:
            self.status(source, "unavailable", type(e).__name__, 60)
            raise SourceError("来源网络请求失败，请稍后重试") from e

    def get(self, url, params=None):
        try:
            return self.request(url, params).json()
        except (ValueError, json.JSONDecodeError) as e:
            raise SourceError("来源没有返回有效数据", "invalid_response") from e

    def details(self, ids):
        result = []
        for offset in range(0, len(ids), 100):
            batch = ids[offset : offset + 100]
            d = self.get("https://music.163.com/api/song/detail/", {"ids": json.dumps([int(i) for i in batch])})
            if d.get("code") != 200:
                raise SourceError("歌曲详情不可读取", "auth_required")
            result += [netease_track(x) for x in d.get("songs", [])]
        return result

    def search(self, query, platform="netease", page=1):
        if platform == "netease":
            d = self.get("https://music.163.com/api/search/get/web", {"s": query, "type": 1, "offset": (page - 1) * 20, "limit": 20})
            if d.get("code") != 200:
                raise SourceError("网易云搜索暂不可用")
            return [netease_track(s) for s in d.get("result", {}).get("songs", [])]
        if platform == "tencent":
            d = self.get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp", {"w": query, "p": page, "n": 20, "format": "json"})
            if d.get("code") != 0:
                raise SourceError("QQ 搜索暂不可用，可粘贴公开歌单导入")
            return [qq_track(s) for s in d.get("data", {}).get("song", {}).get("list", [])]
        if platform == "kuwo":
            d = self.get("https://www.kuwo.cn/search/searchMusicBykeyWord", {"all": query, "pn": page - 1, "rn": 20, "rformat": "json",
                         "encoding": "utf8", "ft": "music", "client": "kt", "mobi": 1, "vipver": 1})
            if not isinstance(d, dict) or "abslist" not in d:
                raise SourceError("酷我搜索暂不可用")
            result = []
            for s in d["abslist"]:
                ident = str(s.get("MUSICRID", "")).removeprefix("MUSIC_")
                if ident.isdigit() and s.get("SONGNAME") and str(s.get("DURATION", "")).isdigit():
                    result.append({"key": "kuwo:" + ident, "platform": "kuwo", "id": ident, "title": html.unescape(s["SONGNAME"]),
                                   "artists": [html.unescape(x) for x in s.get("ARTIST", "").split("&") if x],
                                   "album": html.unescape(s.get("ALBUM", "")), "duration": int(s["DURATION"]),
                                   "cover": "", "url": "https://www.kuwo.cn/play_detail/" + ident})
            return result
        raise SourceError("该平台暂未接入搜索，请输入歌名使用网易云搜索", "unsupported")

    def resolve(self, text):
        match = re.search(r'https?[^\s<>"，]+', text.replace("\\:", ":"))
        if not match:
            raise SourceError("请粘贴网易云或 QQ 音乐歌单分享链接", "invalid_url")
        url = match[0].rstrip(")）")
        validate_url(url)
        u = urlparse(url)
        if u.hostname == 'music.apple.com':
            ident = re.search(r'/playlist/(?:[^/]+/)?(pl\.[a-f0-9]{32})(?:$|[/?])', u.path)
            if ident and u.path.startswith('/cn/'):
                return 'apple', ident[1]
        if u.hostname == 'open.spotify.com':
            ident = re.search(r'/playlist/([A-Za-z0-9]{22})(?:$|/)', u.path)
            if ident:
                return 'spotify', ident[1]
        if u.hostname == 'y.qq.com':
            ident = re.search(r'/toplist/(\d+)', u.path)
            if ident:
                return 'tencent', 'chart:' + ident[1]
        if u.hostname in ("163cn.tv", "c.y.qq.com") and ("163cn.tv" in url or "/base/fcgi-bin/u?" in url):
            url = str(self.request(url).url)
            u = urlparse(url)
        q = parse_qs((u.query or "") + "&" + (u.fragment.partition("?")[2]))
        if u.hostname in ("music.163.com", "y.music.163.com"):
            ident = q.get("id", [""])[0]
            if ident.isdigit() and ("playlist" in url):
                return "netease", ident
        if u.hostname in ("y.qq.com", "c.y.qq.com"):
            ident = (q.get("id") or q.get("disstid") or [""])[0]
            path_id = re.search(r"/(?:playlist|taoge)/(\d+)", u.path)
            ident = path_id[1] if path_id else ident
            if ident.isdigit():
                return "tencent", ident
        raise SourceError("未识别歌单地址，请复制完整歌单分享链接", "invalid_url")

    def playlist(self, platform, ident):
        if platform in ('apple', 'spotify') or (platform == 'tencent' and ident.startswith('chart:')):
            from .charts import read
            return read(self, platform, ident)
        if platform == "netease":
            d = self.get("https://music.163.com/api/v6/playlist/detail", {"id": ident, "n": 100000, "s": 0})
            p = d.get("playlist")
            if d.get("code") != 200 or not p:
                raise SourceError("歌单不可访问，可能为私有歌单或登录已失效", "auth_required")
            ids = [str(x["id"]) for x in p.get("trackIds", [])]
            count = p.get("trackCount")
            if not ids or count != len(ids) or len(set(ids)) != len(ids):
                raise SourceError("歌单为空或成员列表不完整，保留上次快照", "incomplete")
            if count > 10000:
                raise SourceError("第一版单张歌单最多 10000 首", "too_large")
            tracks = {str(s["id"]): netease_track(s) for s in p.get("tracks", [])}
            missing = [i for i in ids if i not in tracks]
            tracks.update({t["id"]: t for t in self.details(missing)})
            if any(i not in tracks for i in ids):
                raise SourceError("部分歌曲详情未返回，保留上次完整快照", "incomplete")
            return {
                "platform": platform,
                "source_id": ident,
                "name": "网易云热歌榜" if str(ident) == "3778678" else p["name"],
                "source_kind": "chart" if str(ident) == "3778678" else "playlist",
                "cover": p.get("coverImgUrl", ""),
                "url": f"https://music.163.com/playlist?id={ident}",
                "tracks": [tracks[i] for i in ids],
                "count": count,
            }
        if platform == "tencent":
            d = self.get(
                "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
                {"type": 1, "json": 1, "utf8": 1, "onlysong": 0, "disstid": ident, "format": "json"},
            )
            if d.get("code") != 0 or not d.get("cdlist"):
                raise SourceError("QQ 歌单不可访问或需要登录", "auth_required")
            p = d["cdlist"][0]
            songs = p.get("songlist", [])
            total = p.get("total_song_num", p.get("songnum"))
            # 该接口若截断返回则拒绝导入，不能把第一页当作完整歌单。
            if not songs or total != len(songs):
                raise SourceError("QQ 未返回全部歌曲，拒绝覆盖快照；请使用完整歌单", "incomplete")
            tracks = [qq_track(s) for s in songs]
            if len({t["key"] for t in tracks}) != total:
                raise SourceError("QQ 返回重复成员，暂不提交快照", "incomplete")
            return {
                "platform": platform,
                "source_id": ident,
                "name": html.unescape(p["dissname"]),
                "cover": p.get("logo", ""),
                "url": f"https://y.qq.com/n/ryqq/playlist/{ident}",
                "tracks": tracks,
                "count": total,
            }
        raise SourceError("汽水暂以手动歌名检索接入，尚不支持自动歌单", "unsupported")

    def lyrics(self, track):
        from .media import build_lrc
        attempts, fallback = [], {}
        for provider in self.db.get("lyric_providers", ["platform", "lrclib"]):
            try:
                raw = self.platform_lyrics(track) if provider == "platform" else self.lrclib_lyrics(track)
                _, state = build_lrc(raw, track.get("duration") or 3600)
                attempts.append({"source": provider, "status": state["status"], "detail": state["label"]})
                if state["status"] in ("synced", "instrumental"):
                    return {**raw, "_source": provider, "_attempts": attempts}
                if not fallback and raw.get("lrc", {}).get("lyric"):
                    fallback = raw
            except (SourceError, ValueError) as e:
                attempts.append({"source": provider, "status": e.code if isinstance(e, SourceError) else "invalid_lyrics",
                                 "detail": str(e) if isinstance(e, SourceError) else "歌词格式无效"})
        return {**fallback, "_attempts": attempts}

    def lrclib_lyrics(self, track):
        if not track.get("duration") or not track.get("artists"):
            raise SourceError("缺少歌词匹配所需的歌手或时长", "incomplete")
        artists = ", ".join(track["artists"])
        d = self.get("https://lrclib.net/api/get", {"track_name": track["title"], "artist_name": artists,
                     "album_name": track.get("album", ""), "duration": track["duration"]})
        if not isinstance(d, dict) or not d.get("trackName"):
            raise SourceError("LRCLIB 未找到匹配歌词", "not_found")
        matches = normalize(d["trackName"]) == normalize(track["title"]) and normalize(d.get("artistName", "")) == normalize(artists)
        matches = matches and isinstance(d.get("duration"), (int, float)) and abs(d["duration"] - track["duration"]) <= 2
        if track.get("album"):
            matches = matches and normalize(d.get("albumName", "")) == normalize(track["album"])
        if not matches:
            raise SourceError("LRCLIB 返回的歌曲版本不完全匹配，已跳过", "version_mismatch")
        if d.get("instrumental"):
            return {"nolyric": True}
        return {"lrc": {"lyric": d.get("syncedLyrics") or d.get("plainLyrics") or ""}}

    def platform_lyrics(self, track):
        if track["platform"] == "netease":
            d = self.get("https://music.163.com/api/song/lyric", {"id": track["id"], "lv": -1, "kv": -1, "tv": -1, "rv": -1})
            if d.get("code") != 200:
                raise SourceError("歌词来源暂不可用")
            return d
        if track["platform"] == "tencent":
            d = self.get(
                "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg", {"songmid": track["id"], "format": "json", "nobase64": 1}
            )
            if d.get("code") != 0:
                raise SourceError("QQ 歌词暂不可用")

            def decode(v):
                v = html.unescape(v or "")
                try:
                    return v if "[" in v else base64.b64decode(v).decode("utf8")
                except Exception:
                    return ""

            return {"lrc": {"lyric": decode(d.get("lyric"))}, "tlyric": {"lyric": decode(d.get("trans"))}}
        return {}

    def audio_url(self, track, provider, bitrate=999):
        if provider.startswith("lx:"):
            if self.lx is None:
                raise SourceError("LX 执行服务尚未配置", "runner_unavailable")
            return self.lx.resolve(provider[3:], track, bitrate)
        if provider == "tencent_direct":
            if track["platform"] != "tencent":
                raise SourceError("QQ 公开音频仅适用于 QQ 歌曲；跨平台请先确认对应版本", "unsupported")
            mid = track["id"]
            prefixes = ["M500"] if bitrate == 128 else ["M800", "M500"]
            filenames = [prefix + mid + mid + ".mp3" for prefix in prefixes]
            payload = {"comm": {"uin": 0, "ct": 24, "cv": 4747474, "format": "json", "platform": "yqq.json"},
                       "req_1": {"module": "music.vkey.GetVkey", "method": "UrlGetVkey", "param": {
                           "guid": str(time.time_ns() % 9000000000 + 1000000000), "songmid": [mid] * len(filenames),
                           "songtype": [0] * len(filenames), "uin": "0", "loginflag": 1, "platform": "20", "filename": filenames}}}
            try:
                d = self.request("https://u.y.qq.com/cgi-bin/musicu.fcg", payload=payload).json()
                section = d.get("req_1", {})
                if d.get("code") != 0 or section.get("code") != 0:
                    raise SourceError("QQ 音频接口暂不可用", "unavailable")
                items = section.get("data", {}).get("midurlinfo", [])
                for filename in filenames:
                    for item in items:
                        if item.get("filename") == filename and item.get("purl"):
                            return {"url": validate_url("https://ws.stream.qqmusic.qq.com/" + item["purl"], media=True),
                                    "quality": "320k" if filename.startswith("M800") else "128k"}
            except (ValueError, AttributeError, TypeError) as e:
                raise SourceError("QQ 音频接口没有返回有效数据", "invalid_response") from e
            raise SourceError("QQ 未提供公开整曲，可能需要登录或会员授权", "auth_required")
        if provider == "netease_direct":
            if track["platform"] != "netease":
                raise SourceError("仅适用于网易云歌曲", "unsupported")
            return {"url": f"https://music.163.com/song/media/outer/url?id={track['id']}.mp3", "quality": "以实测为准"}
        if provider == "gd":
            d = self.get(GD, {"types": "url", "source": track["platform"], "id": track["id"], "br": bitrate})
            if not isinstance(d, dict) or not d.get("url"):
                raise SourceError("该来源未提供完整音频链接", "not_found")
            return {"url": d["url"], "quality": str(d.get("br", "未知"))}
        raise SourceError("该来源需要手动获取后投递文件", "manual")

    def download(self, url, target, cancel=lambda: None, max_bytes=512 * 1024 * 1024):
        validate_url(url, media=True)
        total = 0
        for _ in range(6):
            with self.client.stream("GET", url, headers={"Referer": "https://music.163.com/"}) as r:
                if r.is_redirect:
                    url = str(r.next_request.url)
                    validate_url(url, media=True)
                    continue
                r.raise_for_status()
                length = int(r.headers.get("content-length") or 0)
                if length > max_bytes:
                    raise SourceError("单个文件超过本次获取的大小限制", "too_large")
                with target.open("wb") as f:
                    for data in r.iter_bytes(256 * 1024):
                        cancel()
                        total += len(data)
                        if total > max_bytes:
                            raise SourceError("单个文件超过本次获取的大小限制", "too_large")
                        f.write(data)
                if length and length != total:
                    raise SourceError("下载字节数不完整", "incomplete")
                return total
        raise SourceError("音频链接重定向异常", "invalid_url")
