"""外部脚本专用网络边界：仅公网、固定解析地址、逐跳核验、限制响应大小。"""

import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlencode, urljoin, urlsplit


class PublicHTTPError(ValueError):
    pass


def public_target(url):
    try:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.port not in (None, 80, 443):
            raise PublicHTTPError("只接受 HTTP/HTTPS 公网链接和标准端口")
        host = u.hostname.encode("idna").decode("ascii")
        addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
            raise PublicHTTPError("禁止访问本机、内网及保留地址")
        return u, host, addresses
    except (ValueError, OSError) as e:
        if isinstance(e, PublicHTTPError):
            raise
        raise PublicHTTPError("外部地址无法安全解析") from e


def open_public(url, *, method="GET", headers=None, body=None, timeout=12):
    if method not in ("GET", "POST"):
        raise PublicHTTPError("脚本仅支持 GET/POST 请求")
    headers = {str(k): str(v) for k, v in (headers or {}).items() if str(k).lower() not in
               ("host", "connection", "content-length", "transfer-encoding", "accept-encoding", "proxy-authorization")}
    headers.setdefault("User-Agent", "Liusheng/1.0.0")
    headers["Accept-Encoding"] = "identity"
    headers["Connection"] = "close"
    for _ in range(5):
        u, host, addresses = public_target(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        # 连接已核验的 IP；TLS SNI 和 Host 仍使用原域名，避免 DNS 重绑定。
        sock = socket.create_connection((addresses[0], port), timeout=timeout)
        try:
            if u.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            connection.sock = sock
            connection.request(method, (u.path or "/") + ("?" + u.query if u.query else ""), body, headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                connection.close()
                if not location:
                    raise PublicHTTPError("重定向缺少目标")
                new_url = urljoin(url, location)
                if urlsplit(new_url).hostname != host:
                    headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie", "x-request-key")}
                url = new_url
                if response.status in (301, 302, 303):
                    method, body = "GET", None
                continue
            return response, connection
        except BaseException:
            sock.close()
            connection.close()
            raise
    raise PublicHTTPError("重定向次数过多")


def fetch_public(url, options=None, limit=2 * 1024 * 1024):
    options = options or {}
    headers = dict(options.get("headers") or {})
    body = options.get("body")
    if options.get("form") is not None:
        body = urlencode(options["form"])
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
        headers.setdefault("Content-Type", "application/json")
    if body is not None:
        body = str(body).encode()
        if len(body) > 128 * 1024:
            raise PublicHTTPError("请求体过大")
    response, connection = open_public(url, method=options.get("method", "GET").upper(), headers=headers, body=body)
    try:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise PublicHTTPError("外部响应超过大小限制")
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()
