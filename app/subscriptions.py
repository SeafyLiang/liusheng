"""固定周计划与累计歌单，旧订阅默认行为不变。"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
SCHEDULES = ('manual', 'daily', 'weekly_monday_0400')
RETENTIONS = ('latest', 'append')


def next_check(schedule, timestamp):
    if schedule == 'manual':
        return None
    if schedule == 'daily':
        return timestamp + 86400
    if schedule != 'weekly_monday_0400':
        raise ValueError('更新规则无效')
    at = datetime.fromtimestamp(timestamp, ZONE)
    target = (at + timedelta(days=(7 - at.weekday()) % 7)).replace(hour=4, minute=0, second=0, microsecond=0)
    if target <= at:
        target += timedelta(days=7)
    return int(target.timestamp())


def validate_options(schedule, retention):
    if schedule not in SCHEDULES or retention not in RETENTIONS:
        raise ValueError('请选择有效的更新规则与成员保留策略')


def merge_members(old, fresh, retention):
    if retention == 'latest':
        return fresh
    by_key = {t['key']: t for t in fresh}
    seen = {t['key'] for t in old}
    return [by_key.get(t['key'], t) for t in old] + [t for t in fresh if t['key'] not in seen]


def counts(db, playlist):
    members = json.loads(playlist['snapshot'])
    matches = {r['original']: r['state'] for r in db.rows('SELECT original,state FROM track_matches')}
    return {'current_count': len(json.loads(playlist.get('latest_snapshot') or playlist['snapshot'])),
            'pending_match': sum(matches.get(t['key']) == 'review' for t in members),
            'download_failed': sum(matches.get(t['key']) == 'failed' for t in members)}
