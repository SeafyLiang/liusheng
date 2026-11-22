"""独立容器内的单并发 LX 执行入口；不挂载数据库、凭据或曲库。"""

import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
TOKEN = os.environ.get("LX_RUNNER_TOKEN", "")


def run_script(payload):
    try:
        result = subprocess.run([sys.executable, "-m", "lx_runner.execute"], input=json.dumps(payload, ensure_ascii=False).encode(),
                                capture_output=True, timeout=35, env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        if result.returncode:
            return {"ok": False, "code": "resource_limit"}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "timeout"}
    except Exception:
        return {"ok": False, "code": "runner_error"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value):
        content = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self.reply(200 if self.path == "/health" else 404, {"status": "ok"})

    def do_POST(self):
        self.connection.settimeout(10)
        if self.path != "/run" or not TOKEN or not hmac.compare_digest(TOKEN, self.headers.get("X-Runner-Token", "")):
            return self.reply(403, {"ok": False, "code": "forbidden"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 600000:
                return self.reply(413, {"ok": False, "code": "too_large"})
            if not LOCK.acquire(blocking=False):
                return self.reply(429, {"ok": False, "code": "busy"})
            try:
                payload = json.loads(self.rfile.read(size))
                return self.reply(200, run_script(payload))
            finally:
                LOCK.release()
        except (ValueError, OSError):
            return self.reply(400, {"ok": False, "code": "invalid_request"})


if __name__ == "__main__":
    if len(TOKEN) < 32:
        raise SystemExit("LX_RUNNER_TOKEN 必须配置为至少 32 字符的随机值")
    ThreadingHTTPServer((os.getenv("LX_BIND", "0.0.0.0"), int(os.getenv("LX_PORT", "8090"))), Handler).serve_forever()
