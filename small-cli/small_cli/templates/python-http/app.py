"""A small app created by `small init`. Deploy it with `small deploy`.

Who gets in is decided by magic_link.py: people you invite by work email (`small access add ivan@company.com`)
or with a personal invite link (`small access add bob`). Write your own code in app() — it is a plain WSGI app.
"""

import os
import re
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from magic_link import MagicLink

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>small app</title>
</head>
<body style="font-family: system-ui, sans-serif; text-align: center; padding: 48px 16px;">
  <h1>🚀 It works!</h1>
  <p>This app was created by <code>small init</code>. Edit <code>app.py</code> and run <code>small deploy</code>.</p>
</body>
</html>
""".encode("utf-8")


def app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(PAGE)))])
    return [PAGE]


class ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Handler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # Keep ?token=... (invite link) and ?t=... (sign-in link from an email) out of the logs.
        super().log_message(format, *(re.sub(r"\b(token|t)=[^&\s]*", r"\1=***", str(a)) for a in args))


if __name__ == "__main__":
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", 8080))
    print("Listening on %s:%d" % (host, port), flush=True)
    make_server(host, port, MagicLink(app), server_class=ThreadingServer, handler_class=Handler).serve_forever()
