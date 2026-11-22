"""自动跨平台映射只接受唯一且元数据充分的同一录音。"""
import json
import unicodedata

from .db import now
from .sources import SourceError


def normalized(value):
    # 保留括号和标点；不移除Live/翻唱等版本，不猜测艺名别名。
    return ''.join(unicodedata.normalize('NFKC', value or '').casefold().split())


def same_recording(a, b):
    if normalized(a.get('title')) != normalized(b.get('title')):
        return False
    if not a.get('artists') or {normalized(x) for x in a['artists']} != {normalized(x) for x in b.get('artists', [])}:
        return False
    if not a.get('duration') or not b.get('duration') or abs(a['duration'] - b['duration']) > 2:
        return False
    if a.get('isrc') and b.get('isrc'):
        return a['isrc'].upper() == b['isrc'].upper()
    return not (a.get('album') and b.get('album')) or normalized(a['album']) == normalized(b['album'])


def state(db, original, status, detail):
    db.execute('INSERT INTO track_matches VALUES(?,?,?,?) ON CONFLICT(original) DO UPDATE SET state=excluded.state,detail=excluded.detail,updated=excluded.updated',
               (original, status, detail[:240], now()))


def resolve(engine, original):
    target = engine.canonical(original['key'])
    if target != original['key']:
        return engine.db.track(target)
    # 已下载版本优先；多个录音ID仍要求唯一，不按搜索顺序猜测。
    local = {}
    for row in engine.db.rows('SELECT DISTINCT tracks.key,tracks.data FROM tracks JOIN assets ON assets.track_key=tracks.key WHERE assets.present=1'):
        t = json.loads(row['data'])
        if t['platform'] in ('netease', 'tencent') and same_recording(original, t):
            local[t['key']] = t
    if len(local) > 1:
        state(engine.db, original['key'], 'review', '库内同名版本不唯一，请确认录音版本')
        return None
    if len(local) == 1:
        candidates = local
    else:
        candidates = {}
        failed = False
        for platform in ('netease', 'tencent'):
            try:
                items = engine.sources.search((original['title'] + ' ' + ' '.join(original['artists']))[:160], platform)
            except SourceError:
                failed = True
                continue
            matches = {t['key']: t for t in items if same_recording(original, t)}
            # 平台内唯一即可采用，平台间按网易云、QQ顺序；同一平台多候选必须人工确认。
            if len(matches) > 1:
                state(engine.db, original['key'], 'review', '同平台有多个相同元数据候选，请确认录音版本')
                return None
            if len(matches) == 1:
                candidates = matches
                break
        if not candidates:
            if failed:
                raise SourceError('跨平台搜索暂不可用，稍后重试', 'unavailable')
            state(engine.db, original['key'], 'review', '没有唯一可靠的同录音版本，请选择候选')
            return None
    if len(candidates) != 1:
        state(engine.db, original['key'], 'review', '库内同名版本不唯一，请确认录音版本')
        return None
    track = next(iter(candidates.values()))
    with engine.db.connect() as c:
        engine.db.save_track(track, c)
        # 用户可能在联网期间确认了映射，自动匹配不覆盖。
        c.execute('INSERT OR IGNORE INTO mappings VALUES(?,?,?)', (original['key'], track['key'], now()))
    state(engine.db, original['key'], 'matched', '严格元数据匹配通过')
    return engine.db.track(engine.canonical(original['key']))
