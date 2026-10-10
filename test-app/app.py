import os
import re
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from magic_link import MagicLink

port = int(os.environ.get("PORT", 8080))

HTML = """
<html>
    <head><title>Small Software Demo</title></head>
    <body style="font-family: sans-serif; text-align: center; padding: 50px;">
        <h1>🚀 It works!</h1>
        <p>Это первое микро-приложение, запущенное через нашу платформу Small Software.</p>
    </body>
</html>
""".encode("utf-8")


def app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(HTML)))])
    return [HTML]


class ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Handler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # Не пишем ?token=... в логи Railway.
        super().log_message(format, *(re.sub(r"token=[^&\s]*", "token=***", str(a)) for a in args))


print(f"Starting server on port {port}...", flush=True)
make_server("0.0.0.0", port, MagicLink(app), server_class=ThreadingServer, handler_class=Handler).serve_forever()
