"""Smoke test of the real gateway image (Caddy + magic_link), run by CI after docker run.

Usage: python gateway_smoke.py http://127.0.0.1:8080 KEY
The gateway is started with ACCESS_TOKEN=bob:tok-b and UPSTREAM pointing at an echo app that answers with the request
headers it got as JSON, and with 101 to a websocket upgrade. Exits non-zero on the first failed check.
"""

import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import sys
import time
from urllib.parse import urlsplit

BASE, KEY = sys.argv[1].rstrip("/"), sys.argv[2]
HOST, PORT = urlsplit(BASE).hostname, urlsplit(BASE).port


def request(method, path, headers=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.request(method, path, headers=headers or {})
    res = conn.getresponse()
    return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read().decode("utf-8", "replace")


def check(name, ok, detail=""):
    print(("ok    " if ok else "FAIL  ") + name + ("" if ok else "  " + str(detail)[:300]), flush=True)
    if not ok:
        sys.exit(1)


def signature_ok(user, method, auth):
    moment = auth.split(".")[0]
    mac = hmac.new(KEY.encode(), ("small-gateway|%s|%s|%s" % (user, method, moment)).encode(), hashlib.sha256)
    return auth == "%s.%s" % (moment, base64.urlsafe_b64encode(mac.digest()).decode().rstrip("="))


def upgrade(cookie=None):
    sock = socket.create_connection((HOST, PORT), timeout=15)
    lines = ["GET /ws HTTP/1.1", "Host: %s:%s" % (HOST, PORT), "Upgrade: websocket", "Connection: Upgrade",
             "Sec-WebSocket-Key: " + base64.b64encode(os.urandom(16)).decode(), "Sec-WebSocket-Version: 13"]
    if cookie:
        lines.append("Cookie: " + cookie)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    answer = sock.recv(4096).decode("latin-1")
    sock.close()
    return answer.split("\r\n")[0]


for _ in range(60):  # Caddy and the sign-in server are starting
    try:
        request("GET", "/")
        break
    except OSError:
        time.sleep(1)

status, headers, body = request("GET", "/reports?x=1")
check("no session: the sign-in page", status == 403 and "<form" in body, (status, body[:100]))
check("sign-in page headers", headers.get("x-frame-options") == "DENY"
      and "default-src 'none'" in headers.get("content-security-policy", ""), headers)
check("the check endpoint is closed to visitors", request("GET", "/_access/check")[0] == 404)
check("a spoofed identity without a session gets nowhere",
      request("GET", "/", {"X-Small-User": "boss@evil.example", "X-Small-Method": "email",
                           "X-Small-Auth": "1.forged"})[0] == 403)
check("websocket without a session is refused", " 403 " in upgrade() + " ", upgrade())

status, headers, _ = request("GET", "/reports?token=tok-b&x=1")
check("invite link: redirect without the token", status == 302 and headers.get("location") == "/reports?x=1",
      (status, headers))
cookie = headers.get("set-cookie", "").split(";")[0]
check("invite link: HttpOnly cookie", cookie.startswith("small_access=") and "HttpOnly" in headers.get("set-cookie", ""))

status, _, body = request("GET", "/reports?x=1", {"Cookie": cookie, "X-Small-User": "boss@evil.example"})
check("with a session the app answers", status == 200, (status, body[:200]))
seen = {k.lower(): v for k, v in json.loads(body)["headers"].items()}
check("the app gets who it is, not what the visitor claimed", seen.get("x-small-user") == "bob", seen)
check("the app gets the method", seen.get("x-small-method") == "link", seen)
check("the gateway's signature checks out", signature_ok("bob", "link", seen.get("x-small-auth", "")), seen)
check("the original path and host reach the app", json.loads(body)["path"] == "/reports?x=1"
      and seen.get("host", "").startswith(HOST), (body[:200]))
check("websocket with a session goes through", " 101 " in upgrade(cookie) + " ", upgrade(cookie))
check("a request the gateway already sent on is a loop",
      request("GET", "/", {"Cookie": cookie, "X-Small-Gateway": "1"})[0] == 508)
check("an ordinary proxy's Via header is no loop", request("GET", "/", {"Cookie": cookie, "Via": "1.1 Caddy"})[0] == 200)
status, _, body = request("GET", "/_access/logout", {"Cookie": cookie})
check("sign-out page", status == 200 and "<h1>" in body, status)
print("gateway smoke test passed")
