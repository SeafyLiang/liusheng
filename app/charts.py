"""公开榜单元数据；绝不使用试听 URL 作为整曲音源。"""
import base64
import json
import re
from urllib.parse import urlparse

from .sources import SourceError, qq_track

APPLE_ID = 'pl.939cf56e73c44970b81fd9648f859223'
SPOTIFY_ID = '37i9dQZF1DXahjk49z40fT'
CATALOG = [
    {'platform': 'netease', 'source_id': '3778678', 'name': '网易云热歌榜', 'url': 'https://music.163.com/playlist?id=3778678', 'source_kind': 'chart'},
    {'platform': 'tencent', 'source_id': 'chart:26', 'name': 'QQ 音乐热歌榜', 'url': 'https://y.qq.com/n/ryqq/toplist/26', 'source_kind': 'chart'},
    {'platform': 'apple', 'source_id': APPLE_ID, 'name': 'Apple Music 每周热门100首：中国大陆', 'url': 'https://music.apple.com/cn/playlist/' + APPLE_ID, 'source_kind': 'chart'},
    {'platform': 'spotify', 'source_id': SPOTIFY_ID, 'name': 'Spotify · Mandopop Hits', 'url': 'https://open.spotify.com/playlist/' + SPOTIFY_ID, 'source_kind': 'editorial'},
    {'platform': 'qishui', 'source_id': '', 'name': '汽水官方热歌榜', 'url': 'https://www.douyin.com/qishui/', 'source_kind': 'chart', 'blocked': '官方热歌榜身份与完整读取方式尚未确认，待接入'},
]


def script_data(page, ident, encoded=False):
    for attrs, body in re.findall(r'<script\b([^>]*)>(.*?)</script>', page, re.S):
        if re.search(r'\bid=["\']' + re.escape(ident) + r'["\']', attrs):
            try:
                return json.loads(base64.b64decode(body) if encoded else body)
            except (ValueError, TypeError):
                break
    raise SourceError('页面结构变化，未取得完整榜单', 'incomplete')


def snapshot(platform, ident, name, url, tracks, total, cover='', kind='chart'):
    if not isinstance(total, int) or not 0 < total <= 10000 or len(tracks) != total or len({t['key'] for t in tracks}) != total:
        raise SourceError('榜单成员数量不完整或含重复，保留旧快照', 'incomplete')
    if any(not t.get('title') or not t.get('artists') or t.get('duration', 0) <= 0 for t in tracks):
        raise SourceError('榜单缺少完整歌曲元数据，保留旧快照', 'incomplete')
    return {'platform': platform, 'source_id': ident, 'name': name, 'url': url, 'cover': cover,
            'tracks': tracks, 'count': total, 'source_kind': kind}


def qq_chart(sources, ident):
    chart = ident.removeprefix('chart:')
    if not chart.isdigit():
        raise SourceError('QQ 榜单编号无效', 'invalid_url')
    d = sources.get('https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg', {'topid': chart, 'format': 'json'})
    if d.get('code') != 0 or (chart == '26' and '热歌' not in d.get('topinfo', {}).get('ListName', '')):
        raise SourceError('QQ 未返回目标榜单', 'incomplete')
    tracks = [qq_track(s['data']) for s in d.get('songlist', [])]
    info = d.get('topinfo', {})
    return snapshot('tencent', ident, 'QQ 音乐热歌榜' if chart == '26' else info.get('ListName', ''),
                    'https://y.qq.com/n/ryqq/toplist/' + chart, tracks, d.get('total_song_num'), info.get('pic_album', ''))


def spotify_playlist(sources, ident):
    if not re.fullmatch(r'[A-Za-z0-9]{22}', ident):
        raise SourceError('Spotify 歌单编号无效', 'invalid_url')
    url = 'https://open.spotify.com/playlist/' + ident
    state = script_data(sources.request(url).text, 'initialState', True)
    entity = state.get('entities', {}).get('items', {}).get('spotify:playlist:' + ident, {})
    owner = entity.get('ownerV2', {}).get('data', {}).get('username')
    if ident == SPOTIFY_ID and (owner != 'spotify' or entity.get('name') != 'Mandopop Hits'):
        raise SourceError('Spotify 官方歌单身份未确认', 'incomplete')
    content = entity.get('content', {})
    full = {}
    for item in content.get('items', []):
        t = item.get('itemV2', {}).get('data', {})
        if t.get('uri'):
            full[t['uri']] = t
    embedded = script_data(sources.request('https://open.spotify.com/embed/playlist/' + ident).text, '__NEXT_DATA__')
    e = embedded.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})
    if e.get('id') != ident or e.get('name') != entity.get('name'):
        raise SourceError('Spotify 两个视图身份不一致', 'incomplete')
    items = e.get('trackList', [])
    # 独立页面总数验证嵌入视图完整性，不能将默认50项上限当作完整列表。
    if [t.get('uri') for t in items[:len(full)]] != list(full):
        raise SourceError('Spotify 更新期间视图不一致，请稍后重试', 'incomplete')
    tracks = []
    for item in items:
        uri = item.get('uri', '')
        if not re.fullmatch(r'spotify:track:[A-Za-z0-9]{22}', uri):
            raise SourceError('Spotify 包含无法识别成员', 'incomplete')
        detail = full.get(uri, {})
        artists = [a['profile']['name'] for a in detail.get('artists', {}).get('items', [])]
        tid = uri.rsplit(':', 1)[1]
        tracks.append({'key': 'spotify:' + tid, 'platform': 'spotify', 'id': tid,
                       'title': item['title'], 'artists': artists or [item.get('subtitle', '')],
                       'album': detail.get('albumOfTrack', {}).get('name', ''),
                       'duration': item.get('duration', 0) / 1000, 'cover': '',
                       'url': 'https://open.spotify.com/track/' + tid})
    return snapshot('spotify', ident, 'Spotify · ' + entity.get('name', ''), url, tracks, content.get('totalCount'), kind='editorial')


def read(sources, platform, ident):
    try:
        if platform == 'tencent':
            return qq_chart(sources, ident)
        if platform == 'spotify':
            return spotify_playlist(sources, ident)
        if platform == 'apple':
            return apple_playlist(sources, ident)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise SourceError('榜单结构不完整，旧快照已保留', 'incomplete') from e
    raise SourceError('该榜单尚未接入', 'unsupported')


def apple_playlist(sources, ident):
    if not re.fullmatch(r'pl\.[a-f0-9]{32}', ident):
        raise SourceError('Apple Music 歌单编号无效', 'invalid_url')
    url = 'https://music.apple.com/cn/playlist/' + ident
    response = sources.request(url)
    if not urlparse(str(response.url)).path.startswith('/cn/playlist/'):
        raise SourceError('Apple Music 跳转到非目标页面', 'incomplete')
    root = script_data(response.text, 'serialized-server-data')
    pages = root.get('data', [])
    sections = [section for page in pages for section in page.get('data', {}).get('sections', [])]
    headers = [s['items'][0] for s in sections if s.get('itemKind') == 'containerDetailHeaderLockup' and s.get('items')]
    header = next((h for h in headers if h.get('contentDescriptor', {}).get('identifiers', {}).get('storeAdamID') == ident), None)
    if not header or (ident == APPLE_ID and (header.get('title') != '每周热门 100 首：中国大陆' or
                                           not any(x.get('title') == 'Apple Music' for x in header.get('subtitleLinks', [])))):
        raise SourceError('Apple Music 中国大陆榜单身份未确认', 'incomplete')
    tracks = []
    for section in sections:
        if section.get('id') != 'track-list - ' + ident or section.get('itemKind') != 'trackLockup':
            continue
        for item in section.get('items', []):
            descriptor = item['contentDescriptor']
            if descriptor.get('kind') != 'song':
                raise SourceError('Apple Music 包含非歌曲条目', 'incomplete')
            tid = str(descriptor['identifiers']['storeAdamID'])
            tracks.append({'key': 'apple:' + tid, 'platform': 'apple', 'id': tid, 'title': item['title'],
                           'artists': [a['title'] for a in item.get('subtitleLinks', [])],
                           'album': next((a['title'] for a in item.get('tertiaryLinks', [])), ''),
                           'duration': item.get('duration', 0) / 1000, 'cover': '', 'url': descriptor['url']})
    return snapshot('apple', ident, 'Apple Music · ' + header['title'], url, tracks, header.get('trackCount'))
