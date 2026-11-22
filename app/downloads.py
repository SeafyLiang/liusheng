"""下载版本持久化，原文件只读，准备任务串行且有空间上限。"""
import hashlib
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException

from . import media
from .config import within
from .db import dumps, now


class Downloads:
    limit = 2 * 1024**3

    def __init__(self, cfg, db, personal):
        self.cfg, self.db, self.personal = cfg, db, personal
        self.root = cfg.data / 'offline-files'
        self.root.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='offline')
        self.pending = set()
        with db.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS offline_downloads(
                id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,version TEXT NOT NULL,profile TEXT NOT NULL,
                state TEXT NOT NULL,data TEXT NOT NULL,error TEXT,updated INTEGER NOT NULL)''')
            c.execute("UPDATE offline_downloads SET state='queued' WHERE state='preparing'")
        for row in db.rows("SELECT id FROM offline_downloads WHERE state='queued'"):
            self._queue(row['id'])

    def close(self):
        self.pool.shutdown(wait=True)

    def _queue(self, ident):
        with self.lock:
            if ident not in self.pending:
                self.pending.add(ident)
                self.pool.submit(self._build, ident)

    def prepare(self, sid, profile):
        if profile not in ('original', 'compatible'):
            raise ValueError('未知下载音质')
        song = self.personal.song(sid)
        a = self.db.one('SELECT * FROM assets WHERE id=? AND present=1', (song['asset_id'],))
        if not a:
            raise HTTPException(404, '歌曲文件已不可用')
        source = within(self.cfg.music, a['path'])
        if not source.is_file():
            raise HTTPException(404, '歌曲文件已不可用')
        st = source.stat()
        version = f"{a['id']}:{st.st_mtime_ns}:{st.st_size}:{profile}:1"
        ident = hashlib.sha256(version.encode()).hexdigest()
        row = self.db.one('SELECT * FROM offline_downloads WHERE id=?', (ident,))
        if row and row['state'] == 'ready':
            data = json.loads(row['data'])
            if (self.root / data['stored_name']).is_file():
                return self.status(ident)
        if not row or row['state'] in ('failed', 'ready'):
            if self.db.one("SELECT count(*) n FROM offline_downloads WHERE state IN ('queued','preparing')")['n'] >= 64:
                raise HTTPException(429, '下载准备队列繁忙，请稍后重试')
            self.db.execute('INSERT INTO offline_downloads VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,error=NULL,updated=excluded.updated',
                            (ident, a['id'], version, profile, 'queued', dumps({'song_id': sid}), None, now()))
        self._queue(ident)
        return self.status(ident)

    def status(self, ident):
        row = self.db.one('SELECT * FROM offline_downloads WHERE id=?', (ident,))
        if not row:
            raise HTTPException(404, '下载版本不存在，请重新准备')
        data = json.loads(row['data'])
        self.db.execute('UPDATE offline_downloads SET updated=? WHERE id=?', (now(), ident))
        return {'id': ident, 'state': row['state'], 'error': row['error'], 'profile': row['profile'],
                **{k: v for k, v in data.items() if k != 'stored_name'}}

    def path(self, ident):
        row = self.db.one("SELECT * FROM offline_downloads WHERE id=? AND state='ready'", (ident,))
        if not row:
            raise HTTPException(409, '下载文件尚未准备好')
        data = json.loads(row['data'])
        path = within(self.root, data['stored_name'])
        if not path.is_file() or path.stat().st_size != data['size']:
            raise HTTPException(410, '下载版本已清理，请重新准备')
        self.db.execute('UPDATE offline_downloads SET updated=? WHERE id=?', (now(), ident))
        return path, data

    def _build(self, ident):
        temp = self.root / (ident + '.part')
        try:
            row = self.db.one('SELECT * FROM offline_downloads WHERE id=?', (ident,))
            self.db.execute("UPDATE offline_downloads SET state='preparing' WHERE id=?", (ident,))
            a = self.db.one('SELECT * FROM assets WHERE id=? AND present=1', (row['asset_id'],))
            if not a:
                raise ValueError('源文件已不存在')
            source = within(self.cfg.music, a['path'])
            st = source.stat()
            expected = f"{a['id']}:{st.st_mtime_ns}:{st.st_size}:{row['profile']}:1"
            if expected != row['version']:
                raise ValueError('源文件已变化，请重新下载')
            # Only stale app-generated copies may be reclaimed; no library file is touched.
            for old in self.db.rows("SELECT id,data FROM offline_downloads WHERE state='ready' AND updated<?", (now() - 7 * 86400,)):
                old_data = json.loads(old['data'])
                within(self.root, old_data['stored_name']).unlink(missing_ok=True)
                self.db.execute("UPDATE offline_downloads SET state='failed',error='下载版本已过期，请重新准备' WHERE id=?", (old['id'],))
            used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
            reserve = st.st_size if row['profile'] == 'original' else max(st.st_size, 128 * 1024**2)
            if used + reserve > self.limit or shutil.disk_usage(self.root).free < reserve + 128 * 1024**2:
                raise ValueError('服务器下载准备空间不足，请稍后重试')
            if row['profile'] == 'compatible':
                media.command(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', str(source), '-map', '0:a:0',
                               '-vn', '-map_metadata', '-1', '-c:a', 'libmp3lame', '-threads', '1', '-b:a', '192k',
                               '-fs', str(128 * 1024**2), '-f', 'mp3', '-y', str(temp)], timeout=180)
                media.probe(temp, expected=json.loads(a['data']).get('duration'), decode=True)
                ext, mime = '.mp3', 'audio/mpeg'
            else:
                shutil.copyfile(source, temp)
                ext = source.suffix.lower()
                mime = {'.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.m4a': 'audio/mp4', '.wav': 'audio/wav', '.ape': 'audio/ape'}.get(ext, 'application/octet-stream')
            after = source.stat()
            if (after.st_mtime_ns, after.st_size) != (st.st_mtime_ns, st.st_size):
                raise ValueError('准备过程中源文件已变化，请重新下载')
            digest = media.sha(temp)
            if row['profile'] == 'original':
                self.db.execute('UPDATE assets SET sha256=? WHERE id=? AND mtime_ns=? AND size=?',
                                (digest, a['id'], st.st_mtime_ns, st.st_size))
            filename = ident + ext
            size = temp.stat().st_size
            if size == 0 or used + size > self.limit:
                raise ValueError('下载文件大小无效或超过缓存空间限制')
            _, _, lyrics = media.read_tags(source)
            meta = json.loads(a['data'])
            data = {'song_id': json.loads(row['data'])['song_id'], 'stored_name': filename,
                    'audio_url': f'/api/v1/downloads/{ident}/audio', 'version': digest, 'sha256': digest,
                    'size': size, 'mime': mime, 'filename': media.safe_name('、'.join(meta.get('artists') or []) + ' - ' + meta.get('title', '音乐')) + ext,
                    'lyrics': lyrics, 'codec': 'mp3' if row['profile'] == 'compatible' else media.probe(source, decode=False).get('codec_name', ''), 'cover_url': f"/api/covers/{a['id']}" if meta.get('has_cover') else ''}
            os.replace(temp, self.root / filename)
            self.db.execute("UPDATE offline_downloads SET state='ready',data=?,error=NULL,updated=? WHERE id=?", (dumps(data), now(), ident))
        except Exception as exc:
            self.db.execute("UPDATE offline_downloads SET state='failed',error=?,updated=? WHERE id=?",
                            (str(exc) if isinstance(exc, ValueError) else '文件准备失败，请重试', now(), ident))
        finally:
            temp.unlink(missing_ok=True)
            with self.lock:
                self.pending.discard(ident)
