"""账号迁移、设备会话与统一角色边界。令牌只保存摘要。"""
import hashlib
import hmac
import re
import secrets
import threading
import time

from fastapi import HTTPException

from .db import dumps, now


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()


def public_user(row):
    return {k: row[k] for k in ("id", "username", "role", "enabled")}


class Accounts:
    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.throttle = {}
        with db.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','member')),
                enabled INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL)""")
            columns = {r[1] for r in c.execute('PRAGMA table_info(sessions)')}
            for name, declaration in [('user_id', 'TEXT'), ('kind', "TEXT NOT NULL DEFAULT 'web'"),
                                      ('device_name', "TEXT NOT NULL DEFAULT ''"), ('id', 'TEXT')]:
                if name not in columns:
                    c.execute(f'ALTER TABLE sessions ADD COLUMN {name} {declaration}')
            if not c.execute("SELECT 1 FROM settings WHERE key='accounts_migrated'").fetchone():
                auth = db.get('auth')
                c.execute('INSERT INTO users VALUES (?,?,?,?,?,?,?)',
                          (secrets.token_hex(16), 'admin', auth['salt'], auth['hash'], 'admin', 1, now()))
                c.execute('DELETE FROM sessions')
                c.execute('INSERT INTO settings VALUES (?,?)', ('accounts_migrated', 'true'))
            if not c.execute("SELECT 1 FROM settings WHERE key='server_id'").fetchone():
                c.execute('INSERT INTO settings VALUES (?,?)', ('server_id', dumps(secrets.token_hex(16))))

    def session(self, request):
        authorization = request.headers.get('authorization', '')
        bearer = authorization.startswith('Bearer ')
        token = authorization[7:] if bearer else request.cookies.get('music_session', '')
        if not token or len(token) > 256:
            return None
        return self.db.one("""SELECT s.*,u.username,u.role,u.enabled FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.enabled=1 AND s.kind=?""",
            (hashlib.sha256(token.encode()).hexdigest(), now(), 'device' if bearer else 'web'))

    def login(self, username, password, device, request, kind='web'):
        ip = request.client.host if request.client else 'unknown'
        with self.lock:
            recent = [t for t in self.throttle.get(ip, []) if t > time.time() - 300]
            if len(recent) >= 8:
                raise HTTPException(429, '尝试过于频繁，请 5 分钟后再试')
            self.throttle[ip] = recent + [time.time()]
        user = self.db.one('SELECT * FROM users WHERE username=? COLLATE NOCASE', (username,))
        salt = user['salt'] if user else '00' * 16
        digest = password_hash(password, salt)
        if not user or not user['enabled'] or not hmac.compare_digest(digest, user['password_hash']):
            raise HTTPException(401, '用户名或密码不正确')
        with self.lock:
            self.throttle.pop(ip, None)
        token, csrf, sid = secrets.token_urlsafe(32), secrets.token_urlsafe(24), secrets.token_hex(16)
        expires = now() + 30 * 86400
        self.db.execute('INSERT INTO sessions(token,csrf,expires,user_id,kind,device_name,id) VALUES (?,?,?,?,?,?,?)',
                        (hashlib.sha256(token.encode()).hexdigest(), csrf, expires, user['id'], kind, device[:100], sid))
        return {'authenticated': True, 'token': token, 'csrf': csrf, 'expires': expires,
                'session_id': sid, 'user': public_user(user), 'server_id': self.db.get('server_id'), 'api_version': 1}

    def change_password(self, uid, current, password):
        user = self.db.one('SELECT * FROM users WHERE id=?', (uid,))
        if not hmac.compare_digest(password_hash(current, user['salt']), user['password_hash']):
            raise ValueError('原密码不正确')
        self.set_password(uid, password)

    def set_password(self, uid, password):
        if not 12 <= len(password) <= 256:
            raise ValueError('密码需为 12 至 256 位')
        salt = secrets.token_hex(16)
        with self.db.connect() as c:
            c.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?', (salt, password_hash(password, salt), uid))
            c.execute('DELETE FROM sessions WHERE user_id=?', (uid,))

    def create(self, username, password):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', username):
            raise ValueError('用户名需为 3 至 40 位字母、数字、点、下划线或连字符')
        if not 12 <= len(password) <= 256:
            raise ValueError('密码需为 12 至 256 位')
        if self.db.one('SELECT 1 FROM users WHERE username=? COLLATE NOCASE', (username,)):
            raise HTTPException(409, '用户名已存在')
        uid, salt = secrets.token_hex(16), secrets.token_hex(16)
        self.db.execute('INSERT INTO users VALUES (?,?,?,?,?,?,?)',
                        (uid, username, salt, password_hash(password, salt), 'member', 1, now()))
        return public_user(self.db.one('SELECT * FROM users WHERE id=?', (uid,)))


def member_allowed(path, method):
    """默认拒绝旧管理 API；新增 v1 路由仍由所属路由校验管理员。"""
    if path.startswith('/api/v1/'):
        return not path.startswith('/api/v1/admin/')
    if path in ('/api/logout', '/api/password'):
        return method == 'POST'
    if method not in ('GET', 'HEAD'):
        return False
    if path.startswith('/api/covers/'):
        return True
    return bool(re.fullmatch(r'/api/library/[a-zA-Z0-9]+/(audio|detail)', path))
