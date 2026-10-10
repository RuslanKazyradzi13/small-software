"""Микро-приложение, созданное `small init`. Деплой: `small deploy`.

Доступ по ссылке-приглашению https://<домен>/?token=<ACCESS_TOKEN>: токен лежит в small.json,
проверку делает magic_link.py. Свой код пишите в app() — это обычное WSGI-приложение.
"""

import os
import re
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from magic_link import MagicLink

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>small app</title>
</head>
<body style="font-family: system-ui, sans-serif; text-align: center; padding: 48px 16px;">
  <h1>🚀 Работает!</h1>
  <p>Приложение создано командой <code>small init</code>. Измените <code>app.py</code> и выполните <code>small deploy</code>.</p>
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
        # Не пишем ?token=... в логи.
        super().log_message(format, *(re.sub(r"token=[^&\s]*", "token=***", str(a)) for a in args))


if __name__ == "__main__":
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", 8080))
    print("Listening on %s:%d" % (host, port), flush=True)
    make_server(host, port, MagicLink(app), server_class=ThreadingServer, handler_class=Handler).serve_forever()
