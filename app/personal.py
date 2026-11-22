"""共享曲库视图与每用户的可重放同步日志。"""
import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .db import dumps


class Personal:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = None
        with db.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS library_songs(id TEXT PRIMARY KEY,identity TEXT UNIQUE NOT NULL,data TEXT NOT NULL,signature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS library_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,song_id TEXT NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_playlists(user_id TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(user_id,id));
                CREATE TABLE IF NOT EXISTS playlist_history(user_id TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(user_id,id,revision));
                CREATE TABLE IF NOT EXISTS favorites(user_id TEXT NOT NULL,song_id TEXT NOT NULL,liked INTEGER NOT NULL,PRIMARY KEY(user_id,song_id));
                CREATE TABLE IF NOT EXISTS personal_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS personal_cursor ON personal_changes(user_id,seq);
                CREATE TABLE IF NOT EXISTS sync_operations(user_id TEXT NOT NULL,id TEXT NOT NULL,signature TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(user_id,id));
                CREATE TABLE IF NOT EXISTS library_index_state(id INTEGER PRIMARY KEY,full_refresh INTEGER NOT NULL);
                INSERT OR IGNORE INTO library_index_state VALUES(1,1);
                CREATE TABLE IF NOT EXISTS library_dirty(asset_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS library_members(asset_id TEXT PRIMARY KEY,identity TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS library_member_identity ON library_members(identity);
            ''')
            for action, ref in [('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')]:
                c.executescript(f'''
                    CREATE TRIGGER IF NOT EXISTS library_asset_{action.lower()} AFTER {action} ON assets BEGIN
                        INSERT OR IGNORE INTO library_dirty VALUES({ref}.id);
                    END;
                    CREATE TRIGGER IF NOT EXISTS library_mapping_{action.lower()} AFTER {action} ON mappings BEGIN
                        UPDATE library_index_state SET full_refresh=1 WHERE id=1;
                    END;
                ''')

    def start(self):
        self.refresh_library()
        def maintain():
            while not self.stopping.wait(1):
                try:
                    self.refresh_library()
                except Exception:
                    logging.getLogger(__name__).exception('曲库索引更新失败，将重试')
        self.thread = threading.Thread(target=maintain, name='library-index', daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=25)

    def revision(self):
        return self.db.one('SELECT coalesce(max(seq),0) n FROM library_changes')['n']

    def _dirty(self, c):
        return c.execute('SELECT full_refresh FROM library_index_state WHERE id=1').fetchone()[0] or c.execute('SELECT 1 FROM library_dirty LIMIT 1').fetchone()

    def refresh_library(self):
        # The overwhelmingly common read path does not take a write lock or read assets.
        with self.db.connect() as c:
            if not self._dirty(c):
                return
        with self.lock, self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not self._dirty(c):
                return
            mappings = dict(c.execute('SELECT original,target FROM mappings').fetchall())

            def canonical(key):
                visited = set()
                while key in mappings and key not in visited:
                    visited.add(key)
                    key = mappings[key]
                return key

            full = c.execute('SELECT full_refresh FROM library_index_state WHERE id=1').fetchone()[0]
            dirty = c.execute('SELECT a.* FROM assets a JOIN library_dirty d ON a.id=d.asset_id').fetchall()
            affected = {r[0] for r in c.execute('SELECT m.identity FROM library_members m JOIN library_dirty d ON m.asset_id=d.asset_id')}
            memberships = [(a['id'], 'track:' + canonical(a['track_key']) if a['track_key'] else 'local:' + a['id']) for a in dirty]
            affected.update(identity for _, identity in memberships)
            affected.update('track:' + key for key in mappings if 'track:' + canonical(key) in affected)
            c.execute('DELETE FROM library_members WHERE asset_id IN (SELECT asset_id FROM library_dirty)')
            c.executemany('INSERT OR REPLACE INTO library_members VALUES (?,?)', memberships)
            if full:
                rows = c.execute('SELECT * FROM assets ORDER BY present DESC,path,id').fetchall()
                existing = {r['identity']: dict(r) for r in c.execute('SELECT * FROM library_songs')}
                c.execute('DELETE FROM library_members')
            else:
                keys = dumps(list(affected))
                rows = c.execute('SELECT a.* FROM assets a JOIN library_members m ON a.id=m.asset_id WHERE m.identity IN (SELECT value FROM json_each(?)) ORDER BY a.present DESC,a.path,a.id', (keys,)).fetchall()
                existing = {r['identity']: dict(r) for r in c.execute('SELECT * FROM library_songs WHERE identity IN (SELECT value FROM json_each(?))', (keys,))}
            groups = {}
            for row in rows:
                a = dict(row)
                key = 'track:' + canonical(a['track_key']) if a['track_key'] else 'local:' + a['id']
                groups.setdefault(key, []).append(a)
                if full:
                    c.execute('INSERT INTO library_members VALUES (?,?)', (a['id'], key))
            for identity, assets in groups.items():
                old = existing.get(identity)
                if not old:
                    # Enriching a previously local asset must not change its song ID.
                    candidates = [row for key, row in existing.items() if key not in groups
                                  and json.loads(row['data'])['asset_id'] in {a['id'] for a in assets}]
                    if len(candidates) == 1:
                        old = candidates[0]
                        existing.pop(old['identity'])
                        c.execute('UPDATE library_songs SET identity=? WHERE id=?', (identity, old['id']))
                sid = old['id'] if old else uuid.uuid4().hex
                a = assets[0]
                meta = json.loads(a['data'])
                data = {k: meta.get(k, default) for k, default in [
                    ('title', '未知歌曲'), ('artists', []), ('album', ''), ('duration', 0), ('format', '')]}
                data.update(id=sid, asset_id=a['id'], available=bool(a['present']),
                            audio_url=f"/api/library/{a['id']}/audio" if a['present'] else '',
                            cover_url=f"/api/covers/{a['id']}?v={a['mtime_ns']}" if meta.get('has_cover') else '',
                            folders=sorted({x['path'].split('/')[0] if '/' in x['path'] else '根目录' for x in assets}),
                            fingerprint=a.get('sha256'),
                            asset_version=f"{a['id']}:{a['mtime_ns']}:{a['size']}", size=a['size'])
                signature = hashlib.sha256(dumps(data).encode()).hexdigest()
                if old and old['signature'] == signature:
                    continue
                seq = c.execute('INSERT INTO library_changes(song_id,data) VALUES (?,?)', (sid, '{}')).lastrowid
                data['revision'] = seq
                encoded = dumps(data)
                c.execute('UPDATE library_changes SET data=? WHERE seq=?', (encoded, seq))
                c.execute('INSERT INTO library_songs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,signature=excluded.signature',
                          (sid, identity, encoded, signature))
            # Old identities remain addressable after an administrator remaps a source.
            # Keep a missing marker instead of silently removing personal references.
            for identity in existing.keys() - groups.keys():
                old = existing[identity]
                data = json.loads(old['data'])
                destination = 'track:' + canonical(identity[6:]) if identity.startswith('track:') else ''
                target = c.execute('SELECT id,data FROM library_songs WHERE identity=?', (destination,)).fetchone() if destination in groups else None
                if target and target['id'] != old['id']:
                    replacement = dict(json.loads(target['data']), id=old['id'], alias_of=target['id'])
                    replacement.pop('revision', None)
                    compare = dict(data)
                    compare.pop('revision', None)
                    if replacement == compare:
                        continue
                    data = replacement
                elif data['available']:
                    data.update(available=False, audio_url='')
                else:
                    continue
                seq = c.execute('INSERT INTO library_changes(song_id,data) VALUES (?,?)', (old['id'], '{}')).lastrowid
                data['revision'] = seq
                encoded = dumps(data)
                c.execute('UPDATE library_changes SET data=? WHERE seq=?', (encoded, seq))
                c.execute('UPDATE library_songs SET data=?,signature=? WHERE id=?', (encoded, 'missing', old['id']))
            c.execute('DELETE FROM library_dirty')
            c.execute('UPDATE library_index_state SET full_refresh=0 WHERE id=1')

    def song(self, sid):
        row = self.db.one('SELECT data FROM library_songs WHERE id=?', (sid,))
        if not row:
            raise HTTPException(404, '歌曲不存在')
        return json.loads(row['data'])

    def changes(self, uid, cursor=0):
        rows = self.db.rows('SELECT seq,kind,data FROM personal_changes WHERE user_id=? AND seq>? ORDER BY seq LIMIT 500', (uid, cursor))
        return {'changes': [{'seq': r['seq'], 'kind': r['kind'], 'data': json.loads(r['data'])} for r in rows],
                'cursor': rows[-1]['seq'] if rows else cursor, 'has_more': len(rows) == 500}

    def playlists(self, uid):
        return [json.loads(r['data']) for r in self.db.rows('SELECT data FROM personal_playlists WHERE user_id=?', (uid,))
                if not json.loads(r['data'])['deleted']]

    def _save(self, c, uid, playlist):
        encoded = dumps(playlist)
        c.execute('INSERT INTO personal_playlists VALUES (?,?,?) ON CONFLICT(user_id,id) DO UPDATE SET data=excluded.data', (uid, playlist['id'], encoded))
        c.execute('INSERT INTO playlist_history VALUES (?,?,?,?)', (uid, playlist['id'], playlist['revision'], encoded))
        c.execute('INSERT INTO personal_changes(user_id,kind,data) VALUES (?,?,?)', (uid, 'playlist', encoded))

    def _copy(self, c, uid, local, original, device):
        stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
        copy = dict(local, id=uuid.uuid4().hex, revision=1, deleted=False, conflict_of=original,
                    name=f"{local['name'][:100]} · 冲突副本 {device[:30]} {stamp}")
        self._save(c, uid, copy)
        server = json.loads(c.execute('SELECT data FROM personal_playlists WHERE user_id=? AND id=?', (uid, original)).fetchone()[0])
        return {'status': 'conflict', 'playlist': copy, 'server_playlist': server, 'message': '已保留另一台设备的版本，并另存本机修改'}

    @staticmethod
    def _merge(base, local, current):
        b, left, r = base['song_ids'], local['song_ids'], current['song_ids']
        local_renamed, remote_renamed = local['name'] != base['name'], current['name'] != base['name']
        if local_renamed and remote_renamed and local['name'] != current['name']:
            return None
        common_l, common_r = set(b) & set(left), set(b) & set(r)
        local_order = [s for s in left if s in common_l] != [s for s in b if s in common_l]
        remote_order = [s for s in r if s in common_r] != [s for s in b if s in common_r]
        # A reorder against any other membership/order change preserves a copy.
        if (local_order and r != b and left != r) or (remote_order and left != b and left != r):
            return None
        removed = (set(b) - set(left)) | (set(b) - set(r))
        order = left if local_order else r
        songs = [s for s in order if s not in removed]
        songs += [s for s in left if s not in songs and s not in removed]
        return dict(current, name=local['name'] if local_renamed else current['name'], song_ids=songs)

    def apply(self, uid, operations):
        if not isinstance(operations, list) or len(operations) > 100:
            raise ValueError('一次最多同步 100 个操作')
        results = []
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for op in operations:
                if not isinstance(op, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', str(op.get('id', ''))):
                    raise ValueError('操作 ID 无效')
                oid = op['id']
                signature = hashlib.sha256(json.dumps(op, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                prior = c.execute('SELECT * FROM sync_operations WHERE user_id=? AND id=?', (uid, oid)).fetchone()
                if prior:
                    if prior['signature'] != signature:
                        raise HTTPException(409, '同一操作 ID 不能用于不同内容')
                    results.append(json.loads(prior['result']))
                    continue
                kind, payload = op.get('kind'), op.get('payload', {})
                if not isinstance(payload, dict):
                    raise ValueError('同步内容无效')
                if kind == 'favorite.set':
                    sid = str(payload.get('song_id', ''))
                    if not c.execute('SELECT 1 FROM library_songs WHERE id=?', (sid,)).fetchone():
                        raise ValueError('收藏歌曲不存在，请刷新曲库')
                    if type(payload.get('liked')) is not bool:
                        raise ValueError('收藏状态无效')
                    favorite = {'song_id': sid, 'liked': payload['liked']}
                    c.execute('INSERT INTO favorites VALUES (?,?,?) ON CONFLICT(user_id,song_id) DO UPDATE SET liked=excluded.liked',
                              (uid, sid, int(favorite['liked'])))
                    c.execute('INSERT INTO personal_changes(user_id,kind,data) VALUES (?,?,?)', (uid, 'favorite', dumps(favorite)))
                    result = {'status': 'applied', 'favorite': favorite}
                elif kind in ('playlist.save', 'playlist.delete'):
                    result = self._playlist(c, uid, op)
                else:
                    raise ValueError('未知同步操作')
                result['operation_id'] = oid
                c.execute('INSERT INTO sync_operations VALUES (?,?,?,?)', (uid, oid, signature, dumps(result)))
                results.append(result)
        return results

    def _playlist(self, c, uid, op):
        pid, rev = str(op.get('entity_id', '')), op.get('base_revision', 0)
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', pid) or type(rev) is not int or rev < 0:
            raise ValueError('歌单 ID 或版本无效')
        row = c.execute('SELECT data FROM personal_playlists WHERE user_id=? AND id=?', (uid, pid)).fetchone()
        current = json.loads(row[0]) if row else None
        if not current and rev:
            raise HTTPException(404, '个人歌单不存在')
        base_row = c.execute('SELECT data FROM playlist_history WHERE user_id=? AND id=? AND revision=?', (uid, pid, rev)).fetchone()
        if rev and not base_row:
            raise HTTPException(409, '歌单基础版本不存在，请重新同步')
        base = json.loads(base_row[0]) if base_row else {'name': '', 'song_ids': [], 'deleted': False}
        if op['kind'] == 'playlist.delete':
            if not current:
                raise HTTPException(404, '个人歌单不存在')
            if current['deleted']:
                return {'status': 'applied', 'playlist': current}
            if current['revision'] != rev:
                return {'status': 'conflict', 'playlist': current, 'message': '另一台设备已修改歌单，删除未执行'}
            changed = dict(current, deleted=True, revision=rev + 1)
        else:
            payload = op['payload']
            name, ids = payload.get('name'), payload.get('song_ids')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
                raise ValueError('歌单名称需为 1 至 160 字')
            if not isinstance(ids, list) or len(ids) > 10000 or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
                raise ValueError('歌单歌曲必须唯一，最多 10000 首')
            known = {r[0] for r in c.execute('SELECT id FROM library_songs')}
            if not set(ids) <= known:
                raise ValueError('歌单包含不存在的歌曲')
            local = {'id': pid, 'name': name.strip(), 'song_ids': ids, 'deleted': False, 'conflict_of': None}
            if current and (current['deleted'] or rev == 0 or op.get('conflict_copy') is True):
                return self._copy(c, uid, local, pid, str(op.get('device_name', '设备')))
            changed = local
            if current and current['revision'] != rev:
                changed = self._merge(base, local, current)
                if changed is None:
                    return self._copy(c, uid, local, pid, str(op.get('device_name', '设备')))
            changed['revision'] = (current['revision'] if current else 0) + 1
        self._save(c, uid, changed)
        return {'status': 'applied', 'playlist': changed}
