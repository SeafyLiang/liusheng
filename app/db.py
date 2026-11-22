import json
import sqlite3
import time
from contextlib import contextmanager


def now():
    return int(time.time())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path):
        self.path = path
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tracks (key TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS mappings (original TEXT PRIMARY KEY, target TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, track_key TEXT,
                data TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                sha256 TEXT, present INTEGER NOT NULL DEFAULT 1, updated INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS asset_track ON assets(track_key);
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, source_id TEXT NOT NULL,
                name TEXT NOT NULL, source_url TEXT NOT NULL, cover TEXT,
                enabled INTEGER NOT NULL DEFAULT 1, auto_add INTEGER NOT NULL DEFAULT 0,
                snapshot TEXT NOT NULL, last_check INTEGER, next_check INTEGER,
                last_error TEXT, diff TEXT NOT NULL DEFAULT '{}', created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id TEXT NOT NULL,
                data TEXT NOT NULL, diff TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, unique_key TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, title TEXT NOT NULL,
                state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '等待处理',
                progress INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_run INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created INTEGER NOT NULL, updated INTEGER NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS job_active ON jobs(unique_key)
                WHERE state IN ('queued','running','paused');
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                source TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS source_health (
                source TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT,
                checked INTEGER, blocked_until INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS previews (id TEXT PRIMARY KEY, data TEXT NOT NULL, expires INTEGER NOT NULL);
            """)

            columns = {r[1] for r in c.execute("PRAGMA table_info(playlists)")}
            if "folder" not in columns:
                c.execute("ALTER TABLE playlists ADD COLUMN folder TEXT")
            for name, declaration in [('schedule', "TEXT NOT NULL DEFAULT 'manual'"),
                                      ('retention', "TEXT NOT NULL DEFAULT 'latest'"),
                                      ('source_kind', "TEXT NOT NULL DEFAULT 'playlist'"),
                                      ('latest_snapshot', 'TEXT')]:
                if name not in columns:
                    c.execute(f'ALTER TABLE playlists ADD COLUMN {name} {declaration}')
            if 'schedule' not in columns:
                c.execute("UPDATE playlists SET schedule='daily' WHERE platform='netease'")
            c.execute("UPDATE playlists SET latest_snapshot=snapshot WHERE latest_snapshot IS NULL")
            c.execute("CREATE TABLE IF NOT EXISTS subscription_acquired (playlist_id TEXT NOT NULL,track_key TEXT NOT NULL,PRIMARY KEY(playlist_id,track_key))")
            c.execute("CREATE TABLE IF NOT EXISTS track_matches (original TEXT PRIMARY KEY,state TEXT NOT NULL,detail TEXT NOT NULL,updated INTEGER NOT NULL)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS playlist_folder ON playlists(folder) WHERE folder IS NOT NULL")
            c.execute("CREATE INDEX IF NOT EXISTS asset_present_path ON assets(present,path)")
            job_columns = {r[1] for r in c.execute('PRAGMA table_info(jobs)')}
            for name, declaration in [('started_at','INTEGER'), ('run_started_at','INTEGER'),
                                      ('finished_at','INTEGER'), ('elapsed_seconds','INTEGER NOT NULL DEFAULT 0'),
                                      ('timing_partial','INTEGER NOT NULL DEFAULT 0')]:
                if name not in job_columns:
                    c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {declaration}')
            if 'timing_partial' not in job_columns:
                c.execute("UPDATE jobs SET timing_partial=1 WHERE attempts>0 OR state NOT IN ('queued','paused')")
            c.execute('CREATE TABLE IF NOT EXISTS job_runs (id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,started INTEGER NOT NULL,finished INTEGER,outcome TEXT)')
            c.execute('CREATE INDEX IF NOT EXISTS run_started ON job_runs(started)')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.create_function("casefold", 1, lambda x: (x or "").casefold(), deterministic=True)
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def one(self, sql, args=()):
        r = self.rows(sql, args)
        return r[0] if r else None

    def execute(self, sql, args=()):
        with self.connect() as c:
            return c.execute(sql, args).rowcount

    def get(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set(self, key, value):
        self.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def track(self, key):
        r = self.one("SELECT data FROM tracks WHERE key=?", (key,))
        return json.loads(r["data"]) if r else None

    def save_track(self, track, connection=None):
        args = (track["key"], dumps(track), now())
        sql = "INSERT INTO tracks VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data,updated=excluded.updated"
        if connection is not None:
            connection.execute(sql, args)
        else:
            self.execute(sql, args)
