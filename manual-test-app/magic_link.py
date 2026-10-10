"""Защита WSGI-приложения ссылкой-приглашением (magic link). Один файл, без зависимостей, Python 3.8+.

    from magic_link import MagicLink
    application = MagicLink(application)        # Flask: app.wsgi_app = MagicLink(app.wsgi_app)

Владелец раздаёт ссылку https://<app>/?token=<ACCESS_TOKEN>. По ней middleware ставит куку и перенаправляет
на тот же адрес без токена. Без верного токена или куки — страница 403 с кнопкой «Запросить доступ».

Переменные окружения:
    ACCESS_TOKEN        токен доступа (можно несколько через запятую); не задан — доступ закрыт для всех
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


class MagicLink:
    def __init__(self, app, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE):
        if tokens is None:
            tokens = os.environ.get("ACCESS_TOKEN", "")
        if isinstance(tokens, str):
            tokens = tokens.split(",")
        self.app = app
        self.tokens = [t.strip() for t in tokens if t.strip()]
        self.cookies = [_cookie_value(t) for t in self.tokens]
        self.request_url = os.environ.get("ACCESS_REQUEST_URL", "") if request_url is None else request_url
        self.cookie_name = cookie_name
        self.max_age = max_age
        if not self.tokens:
            _log("[magic-link] ACCESS_TOKEN не задан: доступ закрыт для всех")

    def __call__(self, environ, start_response):
        base = environ.get("SCRIPT_NAME", "")
        path = environ.get("PATH_INFO", "") or "/"
        if path == REQUEST_PATH and environ.get("REQUEST_METHOD") == "POST" and not self.request_url:
            return self._access_request(environ, start_response)

        query = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        token = next((value for key, value in query if key == "token"), None)
        if token is not None:
            if not _matches(token, self.tokens):
                return self._page(environ, start_response, "403 Forbidden", "invalid")
            # Убираем токен из адреса, чтобы он не оставался в истории браузера и не утекал через Referer.
            # Ведущие слэши схлопываем: Location "//host" был бы открытым редиректом.
            location = "/" + quote((base + path).encode("latin-1", "replace"), safe="/").lstrip("/")
            rest = urlencode([(k, v) for k, v in query if k != "token"])
            if rest:
                location += "?" + rest
            start_response("302 Found", [
                ("Location", location),
                ("Set-Cookie", self._cookie_header(environ, token)),
                ("Cache-Control", "no-store"),
                ("Content-Length", "0"),
            ])
            return [b""]

        if self._has_valid_cookie(environ):
            return self.app(environ, start_response)
        return self._page(environ, start_response, "403 Forbidden", "forbidden")

    def _has_valid_cookie(self, environ):
        try:
            morsel = SimpleCookie(environ.get("HTTP_COOKIE", "")).get(self.cookie_name)
        except CookieError:
            return False
        return morsel is not None and _matches(morsel.value, self.cookies)

    def _cookie_header(self, environ, token):
        proto = environ.get("HTTP_X_FORWARDED_PROTO") or environ.get("wsgi.url_scheme", "http")
        secure = "; Secure" if proto.split(",")[0].strip() == "https" else ""
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (
            self.cookie_name, _cookie_value(token), self.max_age, secure)

    def _access_request(self, environ, start_response):
        try:
            size = min(int(environ.get("CONTENT_LENGTH") or 0), 4096)
        except ValueError:
            size = 0
        form = dict(parse_qsl(environ["wsgi.input"].read(size).decode("utf-8", "replace")))
        forwarded = environ.get("HTTP_X_FORWARDED_FOR") or environ.get("REMOTE_ADDR", "")
        # repr() экранирует переводы строк: пользователь не сможет подделать строки лога.
        _log("[access-request] contact=%r ip=%s ua=%r" % (
            form.get("contact", "")[:200], forwarded.split(",")[0].strip(),
            environ.get("HTTP_USER_AGENT", "")[:200]))
        return self._page(environ, start_response, "200 OK", "sent")

    def _page(self, environ, start_response, status, kind):
        icon, title, text = MESSAGES[kind]
        if kind == "sent":
            action = ""
        elif self.request_url:
            action = '<a class="btn" href="%s">Запросить доступ</a>' % html.escape(self.request_url)
        else:
            action = REQUEST_FORM.substitute(action=html.escape(environ.get("SCRIPT_NAME", "") + REQUEST_PATH))
        body = PAGE.substitute(icon=icon, title=html.escape(title), text=html.escape(text), action=action).encode("utf-8")
        start_response(status, [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ])
        return [body]


def _cookie_value(token):
    # В куке лежит не сам токен, а производное от него: смена ACCESS_TOKEN отзывает все выданные куки.
    return hmac.new(token.encode("utf-8"), b"magic-link-cookie", hashlib.sha256).hexdigest()


def _matches(value, candidates):
    value = value.encode("utf-8")
    return any(hmac.compare_digest(value, c.encode("utf-8")) for c in candidates)


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
