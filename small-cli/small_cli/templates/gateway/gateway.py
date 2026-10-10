"""The small gateway: sign-in in front of an app that has none of its own (small gateway on).

Caddy (the Caddyfile next to this file) takes every request. Sign-in pages (/_access/...) go to the sign-in server in
this process. Every other request is first shown to it through Caddy's forward_auth: GET /_access/check, with the
original method and URI in X-Forwarded-Method and X-Forwarded-Uri. A 2xx answer lets the request through to the app
over Railway's private network, with X-Small-User, X-Small-Method and X-Small-Auth added (the gateway's signature, see
magic_link.gateway_auth); any other answer — the sign-in page, a redirect that sets the cookie — goes back to the
visitor as is. Everything magic_link does (single sign-on, email links, invite links, limits, logs) happens here, with
the same environment variables; the app itself gets only SMALL_GATEWAY_KEY to check the signature, if it wants to.

This is the container's main process: it starts the sign-in server, then Caddy, and exits when Caddy does, so that
Railway restarts the whole gateway.

Environment: UPSTREAM (http://<app>.railway.internal:<port>), SMALL_GATEWAY_KEY, and magic_link's variables.
"""

import os
import signal
import subprocess
import sys
import threading
from socketserver import ThreadingMixIn
from urllib.parse import unquote_to_bytes, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from magic_link import MagicLink, gateway_auth

AUTH_ADDRESS = ("127.0.0.1", 9000)
CHECK_PATH = "/_access/check"
CADDYFILE = "/etc/caddy/Caddyfile"


def make_app(key):
    """The sign-in server: magic_link in front of an "app" that only answers forward_auth with who got in."""

    def let_in(environ, start_response):
        user, method = environ["magic_link.user"], environ["magic_link.method"]
        headers = [("X-Small-Method", method), ("X-Small-Auth", gateway_auth(key, user, method)),
                   ("Cache-Control", "no-store"), ("Content-Length", "0")]
        if user:  # the shared invite link has no name
            headers.append(("X-Small-User", user))
        start_response("200 OK", headers)
        return [b""]

    # gateway_key="": the gateway signs; it must never trust signatures itself.
    return ForwardAuth(MagicLink(let_in, gateway_key=""))


class ForwardAuth:
    """Makes Caddy's GET /_access/check look to magic_link like the request Caddy is asking about."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO") == CHECK_PATH:
            path, _, query = (environ.get("HTTP_X_FORWARDED_URI") or "/").partition("?")
            path = unquote_to_bytes(path).decode("latin-1") or "/"  # PATH_INFO is decoded, as WSGI servers do
            if path.startswith("/_access/"):  # Caddy sends sign-in pages here directly, never through the check
                start_response("404 Not Found", [("Content-Length", "0")])
                return [b""]
            environ = dict(environ, PATH_INFO=path, QUERY_STRING=query, CONTENT_LENGTH="0",
                           REQUEST_METHOD=environ.get("HTTP_X_FORWARDED_METHOD") or "GET")
        return self.app(environ, start_response)


class ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass  # request lines carry ?token= and sign-in links: magic_link logs what matters, as JSON


def main():
    key, upstream = os.environ.get("SMALL_GATEWAY_KEY", "").strip(), os.environ.get("UPSTREAM", "").strip()
    if not (key and upstream):
        sys.exit("small gateway: SMALL_GATEWAY_KEY and UPSTREAM must be set (small gateway on sets them)")
    if not urlsplit(upstream).hostname:
        # Railway fills in the reference when the gateway deploys; empty means the app had no private address yet.
        # Proxying to "http://:port" would send every request back into the gateway.
        sys.exit("small gateway: UPSTREAM has no host (%s): the app had no private address when the gateway was "
                 "deployed. Redeploy the gateway: small deploy, or Railway's Redeploy button" % upstream)
    server = make_server(*AUTH_ADDRESS, make_app(key), server_class=ThreadingServer, handler_class=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    caddy = subprocess.Popen(["caddy", "run", "--config", CADDYFILE, "--adapter", "caddyfile"])
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: caddy.terminate())
    sys.exit(caddy.wait())


if __name__ == "__main__":
    main()
