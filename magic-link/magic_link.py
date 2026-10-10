"""Защита веб-приложения ссылкой-приглашением (magic link). Один файл, без зависимостей, Python 3.8+.

    from magic_link import MagicLink, MagicLinkASGI
    application = MagicLink(application)        # WSGI: Django; Flask — app.wsgi_app = MagicLink(app.wsgi_app)
    app.add_middleware(MagicLinkASGI)           # ASGI: FastAPI, Starlette; или app = MagicLinkASGI(app)

Владелец раздаёт ссылку https://<app>/?token=<токен>. По ней middleware ставит куку и перенаправляет
на тот же адрес без токена. Без верного токена или куки — страница 403 с кнопкой «Запросить доступ».

Переменные окружения:
    ACCESS_TOKEN        токены через запятую; у токена может быть имя: "alice:токен1,bob:токен2".
                        Убрали чей-то токен — его ссылка и выданные по ней куки перестают работать,
                        у остальных всё работает. Не задан — доступ закрыт для всех.
    ACCESS_REQUEST_URL  куда ведёт кнопка (mailto:..., https://t.me/...). Не задан — кнопка отправляет
                        форму в само приложение, и запрос печатается в лог строкой [access-request]
"""

import hashlib
import hmac
import html
import os
from http.cookies import CookieError, SimpleCookie
from string import Template
from urllib.parse import parse_qsl, quote, urlencode

COOKIE_NAME = "small_access"
COOKIE_MAX_AGE = 30 * 24 * 3600
REQUEST_PATH = "/_access/request"

MESSAGES = {
    "forbidden": ("&#128274;", "Доступ по приглашению",
                  "Эта страница открывается только по ссылке-приглашению от владельца."),
    "invalid": ("&#128274;", "Ссылка недействительна", "Ссылка устарела или скопирована не полностью. Запросите новую."),
    "sent": ("&#9989;", "Запрос отправлен", "Владелец увидит запрос и пришлёт вам ссылку-приглашение."),
}


def parse_tokens(value):
    """"alice:tok1, tok2" -> [("alice", "tok1"), ("", "tok2")]. В самом токене двоеточия быть не должно."""
    items = value.split(",") if isinstance(value, str) else value
    entries = []
    for item in items:
        name, _, token = item.strip().rpartition(":")
        if token.strip():
            entries.append((name.strip(), token.strip()))
    return entries


class _Gate:
    """Общая логика WSGI- и ASGI-версий: токены, куки, страницы, логи."""

    def __init__(self, tokens, request_url, cookie_name, max_age):
        self.entries = parse_tokens(os.environ.get("ACCESS_TOKEN", "") if tokens is None else tokens)
        self.cookies = [(name, _cookie_value(token)) for name, token in self.entries]
        self.request_url = os.environ.get("ACCESS_REQUEST_URL", "") if request_url is None else request_url
        self.cookie_name = cookie_name
        self.max_age = max_age
        if not self.entries:
            _log("[magic-link] ACCESS_TOKEN не задан: доступ закрыт для всех")

    def token_owner(self, token):
        """Имя владельца токена ("" — без имени) или None, если токен неверный."""
        return _owner(token, self.entries)

    def cookie_owner(self, cookie_header):
        try:
            morsel = SimpleCookie(cookie_header or "").get(self.cookie_name)
        except CookieError:
            return None
        return None if morsel is None else _owner(morsel.value, self.cookies)

    def set_cookie(self, token, https):
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (
            self.cookie_name, _cookie_value(token), self.max_age, "; Secure" if https else "")

    @staticmethod
    def location(path_bytes, query):
        # Убираем токен из адреса, чтобы он не оставался в истории браузера и не утекал через Referer.
        # Ведущие слэши схлопываем: Location "//host" был бы открытым редиректом.
        location = "/" + quote(path_bytes, safe="/").lstrip("/")
        rest = urlencode([(k, v) for k, v in query if k != "token"])
        return location + ("?" + rest if rest else "")

    def page(self, kind, base):
        icon, title, text = MESSAGES[kind]
        if kind == "sent":
            action = ""
        elif self.request_url:
            action = '<a class="btn" href="%s">Запросить доступ</a>' % html.escape(self.request_url)
        else:
            action = REQUEST_FORM.substitute(action=html.escape(base + REQUEST_PATH))
        return PAGE.substitute(icon=icon, title=html.escape(title), text=html.escape(text), action=action).encode("utf-8")

    @staticmethod
    def log_request(body, ip, user_agent):
        form = dict(parse_qsl(body.decode("utf-8", "replace")))
        # repr() экранирует переводы строк: пользователь не сможет подделать строки лога.
        _log("[access-request] contact=%r ip=%s ua=%r" % (form.get("contact", "")[:200], ip, user_agent[:200]))

    @staticmethod
    def log_login(name, ip):
        _log("[magic-link] вход по ссылке%s, ip=%s" % (" (%s)" % name if name else "", ip))


class MagicLink:
    """WSGI-middleware."""

    def __init__(self, app, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE):
        self.app = app
        self.gate = _Gate(tokens, request_url, cookie_name, max_age)

    def __call__(self, environ, start_response):
        gate = self.gate
        base = environ.get("SCRIPT_NAME", "")
        path = environ.get("PATH_INFO", "") or "/"
        ip = _client_ip(environ.get("HTTP_X_FORWARDED_FOR"), environ.get("REMOTE_ADDR"))
        if path == REQUEST_PATH and environ.get("REQUEST_METHOD") == "POST" and not gate.request_url:
            try:
                size = min(int(environ.get("CONTENT_LENGTH") or 0), 4096)
            except ValueError:
                size = 0
            gate.log_request(environ["wsgi.input"].read(size), ip, environ.get("HTTP_USER_AGENT", ""))
            return self._page(start_response, "200 OK", gate.page("sent", base))

        query = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        token = next((value for key, value in query if key == "token"), None)
        if token is not None:
            name = gate.token_owner(token)
            if name is None:
                return self._page(start_response, "403 Forbidden", gate.page("invalid", base))
            gate.log_login(name, ip)
            https = _is_https(environ.get("HTTP_X_FORWARDED_PROTO"), environ.get("wsgi.url_scheme"))
            start_response("302 Found", [
                ("Location", gate.location((base + path).encode("latin-1", "replace"), query)),
                ("Set-Cookie", gate.set_cookie(token, https)),
                ("Cache-Control", "no-store"),
                ("Content-Length", "0"),
            ])
            return [b""]

        if gate.cookie_owner(environ.get("HTTP_COOKIE", "")) is not None:
            return self.app(environ, start_response)
        return self._page(start_response, "403 Forbidden", gate.page("forbidden", base))

    @staticmethod
    def _page(start_response, status, body):
        start_response(status, [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ])
        return [body]


class MagicLinkASGI:
    """ASGI-middleware (FastAPI, Starlette, Litestar...). Websocket без доступа закрывается до accept (403)."""

    def __init__(self, app, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE):
        self.app = app
        self.gate = _Gate(tokens, request_url, cookie_name, max_age)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):  # lifespan и прочее — без проверки
            return await self.app(scope, receive, send)
        gate = self.gate
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        cookie_ok = gate.cookie_owner(headers.get("cookie", "")) is not None
        if scope["type"] == "websocket":
            if cookie_ok:
                return await self.app(scope, receive, send)
            return await send({"type": "websocket.close", "code": 1008})

        base = scope.get("root_path", "")
        path = scope.get("path", "") or "/"
        client = scope.get("client") or ("", 0)
        ip = _client_ip(headers.get("x-forwarded-for"), client[0])
        if path == REQUEST_PATH and scope.get("method") == "POST" and not gate.request_url:
            body = b""
            while len(body) < 4096:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            gate.log_request(body[:4096], ip, headers.get("user-agent", ""))
            return await _asgi_page(send, 200, gate.page("sent", base))

        query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        token = next((value for key, value in query if key == "token"), None)
        if token is not None:
            name = gate.token_owner(token)
            if name is None:
                return await _asgi_page(send, 403, gate.page("invalid", base))
            gate.log_login(name, ip)
            https = _is_https(headers.get("x-forwarded-proto"), scope.get("scheme"))
            await send({"type": "http.response.start", "status": 302, "headers": [
                (b"location", gate.location((base + path).encode("utf-8"), query).encode("latin-1")),
                (b"set-cookie", gate.set_cookie(token, https).encode("latin-1")),
                (b"cache-control", b"no-store"),
                (b"content-length", b"0"),
            ]})
            return await send({"type": "http.response.body", "body": b""})

        if cookie_ok:
            return await self.app(scope, receive, send)
        return await _asgi_page(send, 403, gate.page("forbidden", base))


async def _asgi_page(send, status, body):
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]})
    await send({"type": "http.response.body", "body": body})


def _owner(value, entries):
    value = value.encode("utf-8")
    for name, candidate in entries:
        if hmac.compare_digest(value, candidate.encode("utf-8")):
            return name
    return None


def _cookie_value(token):
    # В куке лежит не сам токен, а производное от него: убрали токен из ACCESS_TOKEN — его куки больше не подходят.
    return hmac.new(token.encode("utf-8"), b"magic-link-cookie", hashlib.sha256).hexdigest()


def _client_ip(forwarded, remote):
    return (forwarded or remote or "").split(",")[0].strip()


def _is_https(forwarded_proto, scheme):
    return (forwarded_proto or scheme or "http").split(",")[0].strip() == "https"


def _log(message):
    print(message, flush=True)


REQUEST_FORM = Template(
    '<form method="post" action="$action">'
    '<input name="contact" required maxlength="200" placeholder="Email или Telegram для ответа">'
    '<button class="btn" type="submit">Запросить доступ</button></form>'
)

PAGE = Template("""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>$title</title>
<style>
  :root { color-scheme: light dark; --bg: #f5f5f4; --card: #fff; --text: #1c1917; --muted: #57534e;
          --border: #e7e5e4; --accent: #2563eb; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0c0a09; --card: #1c1917; --text: #f5f5f4; --muted: #a8a29e; --border: #292524; --accent: #3b82f6; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
         background: var(--bg); color: var(--text); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { width: 100%; max-width: 400px; padding: 32px; text-align: center;
         background: var(--card); border: 1px solid var(--border); border-radius: 12px; }
  .icon { font-size: 32px; line-height: 1; margin-bottom: 12px; }
  h1 { margin: 0 0 8px; font-size: 20px; }
  p { margin: 0 0 24px; color: var(--muted); }
  input { width: 100%; margin-bottom: 12px; padding: 10px 12px; font: inherit; color: inherit;
          background: transparent; border: 1px solid var(--border); border-radius: 8px; }
  .btn { display: block; width: 100%; padding: 10px 16px; font: inherit; font-weight: 600; color: #fff;
         background: var(--accent); border: 0; border-radius: 8px; text-decoration: none; cursor: pointer; }
  .btn:hover { filter: brightness(1.1); }
</style>
</head>
<body>
<main>
  <div class="icon" aria-hidden="true">$icon</div>
  <h1>$title</h1>
  <p>$text</p>
  $action
</main>
</body>
</html>
""")
