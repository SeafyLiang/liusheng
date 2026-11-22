"""每次请求单独启动的受限 QuickJS 进程，不向 JS 暴露 Python、文件或进程 API。"""

import base64
import hashlib
import json
import os
import resource
import sys
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit

import quickjs
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

from app.public_http import PublicHTTPError, fetch_public


def execute(payload):
    resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
    ctx = quickjs.Context()
    ctx.set_memory_limit(32 * 1024 * 1024)
    ctx.set_max_stack_size(512 * 1024)
    requests = []

    def as_bytes(value):
        return value.encode() if isinstance(value, str) else bytes(value)

    def native(raw):
        try:
            data = json.loads(raw)
            op, args = data["op"], data["args"]
            if op == "request":
                if len(requests) >= 8:
                    raise ValueError("request_limit")
                url, options = args
                record = {"host": (urlsplit(url).hostname or "")[:100]}
                requests.append(record)
                status, headers, content = fetch_public(url, options)
                record["status"] = status
                body = content.decode("utf8", "replace")
                try:
                    body = json.loads(body)
                except ValueError:
                    pass
                value = {"statusCode": status, "headers": headers, "body": body}
            elif op == "buffer":
                value, encoding = args
                if isinstance(value, str):
                    value = base64.b64decode(value) if encoding == "base64" else bytes.fromhex(value) if encoding == "hex" else value.encode()
                value = list(value)
            elif op == "string":
                value, encoding = args
                value = as_bytes(value)
                value = base64.b64encode(value).decode() if encoding == "base64" else value.hex() if encoding == "hex" else value.decode("utf8", "replace")
            elif op == "md5":
                value = hashlib.md5(as_bytes(args[0])).hexdigest()
            elif op == "randomBytes":
                value = list(os.urandom(min(4096, max(0, int(args[0])))))
            elif op == "aesEncrypt":
                value, mode, key, *iv = args
                if mode not in ("aes-128-cbc", "aes-128-ecb", "aes-256-cbc"):
                    raise ValueError("unsupported_crypto")
                cipher = AES.new(as_bytes(key), AES.MODE_ECB) if mode.endswith("ecb") else AES.new(as_bytes(key), AES.MODE_CBC, as_bytes(iv[0]))
                value = list(cipher.encrypt(pad(as_bytes(value), 16)))
            elif op == "rsaEncrypt":
                value = list(PKCS1_v1_5.new(RSA.import_key(args[1])).encrypt(as_bytes(args[0])))
            elif op == "deflate":
                value = list(zlib.compress(as_bytes(args[0])))
            elif op == "inflate":
                value = zlib.decompressobj().decompress(as_bytes(args[0]), 2 * 1024 * 1024 + 1)
                if len(value) > 2 * 1024 * 1024:
                    raise ValueError("response_limit")
                value = list(value)
            else:
                raise ValueError("unsupported_api")
            return json.dumps({"value": value}, ensure_ascii=False)
        except Exception as e:
            # 不把脚本令牌、请求路径、上游响应或任意异常文本回显给 UI/日志。
            code = "blocked_address" if isinstance(e, PublicHTTPError) else "network_or_api_error"
            if op == "request" and requests:
                requests[-1]["error"] = code
            return json.dumps({"error": code})

    ctx.add_callable("__native", native)
    ctx.set("__scriptInfo", json.dumps({**payload.get("metadata", {}), "rawScript": payload["script"]}))
    ctx.eval(Path(__file__).with_name("bootstrap.js").read_text())
    try:
        ctx.eval(payload["script"])
        deadline = time.monotonic() + 25
        invoked = False
        while time.monotonic() < deadline:
            for _ in range(100):
                if not ctx.execute_pending_job():
                    break
            ctx.eval("__lxTick()")
            state = json.loads(ctx.eval("__lxState()"))
            initialized = state.get("initialized")
            if initialized is not None:
                if not isinstance(initialized, dict) or initialized.get("status") is False or not isinstance(initialized.get("sources"), dict):
                    return {"ok": False, "code": "init_failed", "requests": requests}
                if not payload.get("request"):
                    return {"ok": True, "capabilities": initialized["sources"], "requests": requests}
                if not invoked:
                    ctx.get("__lxInvoke")(json.dumps(payload["request"]))
                    invoked = True
                elif state.get("finished"):
                    return {"ok": not bool(state.get("failure")), "code": state.get("failure"), "url": state.get("result"), "requests": requests}
            time.sleep(0.005)
        return {"ok": False, "code": "timeout", "requests": requests}
    except Exception:
        return {"ok": False, "code": "script_error", "requests": requests}


if __name__ == "__main__":
    print(json.dumps(execute(json.loads(sys.stdin.read(600000))), ensure_ascii=False))
