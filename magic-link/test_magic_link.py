"""Тесты magic_link.py (WSGI и ASGI). Запуск: python -m unittest test_magic_link"""

import asyncio
import contextlib
import io
import unittest
from unittest import mock
from wsgiref.util import setup_testing_defaults

from magic_link import COOKIE_NAME, MagicLink, MagicLinkASGI, _cookie_value, parse_tokens

TOKEN = "s3cret-token"


def secret_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"secret page"]


def call(app, path="/", query="", cookie=None, method="GET", body=b"", headers=None):
    environ = {"PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": method,
               "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body))}
    if cookie is not None:
        environ["HTTP_COOKIE"] = cookie
    environ.update(headers or {})
    setup_testing_defaults(environ)
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        body = b"".join(app(environ, start_response)).decode("utf-8")
    return captured["status"], captured["headers"], body, out.getvalue()


class MagicLinkTest(unittest.TestCase):
    def setUp(self):
        self.app = MagicLink(secret_app, tokens=TOKEN, request_url="")
        self.cookie = "%s=%s" % (COOKIE_NAME, _cookie_value(TOKEN))

    def test_no_token_shows_403_with_request_button(self):
        status, headers, body, _ = call(self.app)
        self.assertEqual(status, "403 Forbidden")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Доступ по приглашению", body)
        self.assertIn("Запросить доступ", body)
        self.assertIn('action="/_access/request"', body)
        self.assertNotIn("secret page", body)

    def test_valid_token_sets_cookie_and_redirects_without_token(self):
        status, headers, _, _ = call(self.app, path="/reports", query="token=%s&page=2" % TOKEN)
        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], "/reports?page=2")
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(self.cookie + ";"))
        self.assertNotIn(TOKEN, cookie)
        for flag in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=2592000"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)

    def test_cookie_is_secure_behind_https_proxy(self):
        _, headers, _, _ = call(self.app, query="token=" + TOKEN, headers={"HTTP_X_FORWARDED_PROTO": "https"})
        self.assertIn("; Secure", headers["Set-Cookie"])

    def test_valid_cookie_passes_through(self):
        status, _, body, _ = call(self.app, cookie="other=1; " + self.cookie)
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, "secret page")

    def test_invalid_token_and_forged_cookie_are_rejected(self):
        status, _, body, _ = call(self.app, query="token=wrong")
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Ссылка недействительна", body)
        for cookie in ("%s=%s" % (COOKIE_NAME, TOKEN), "%s=deadbeef" % COOKIE_NAME, "garbage;;=="):
            self.assertEqual(call(self.app, cookie=cookie)[0], "403 Forbidden", cookie)

    def test_no_open_redirect(self):
        _, headers, _, _ = call(self.app, path="//evil.example/x", query="token=" + TOKEN)
        self.assertEqual(headers["Location"], "/evil.example/x")

    def test_multiple_tokens_from_env(self):
        with mock.patch.dict("os.environ", {"ACCESS_TOKEN": "alice-token, bob-token"}):
            app = MagicLink(secret_app)
        self.assertEqual(call(app, query="token=bob-token")[0], "302 Found")
        self.assertEqual(call(app, query="token=alice-token")[0], "302 Found")
        self.assertEqual(call(app, query="token=carol")[0], "403 Forbidden")

    def test_unconfigured_token_locks_everyone_out(self):
        with mock.patch.dict("os.environ", {"ACCESS_TOKEN": ""}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                app = MagicLink(secret_app)
        self.assertIn("ACCESS_TOKEN не задан", out.getvalue())
        self.assertEqual(call(app)[0], "403 Forbidden")
        self.assertEqual(call(app, query="token=")[0], "403 Forbidden")

    def test_access_request_is_logged(self):
        status, _, body, log = call(self.app, path="/_access/request", method="POST",
                                    body="contact=%40ivan%0A[access-request] fake".encode(),
                                    headers={"HTTP_X_FORWARDED_FOR": "203.0.113.7, 10.0.0.1"})
        self.assertEqual(status, "200 OK")
        self.assertIn("Запрос отправлен", body)
        self.assertNotIn("<form", body)
        self.assertEqual(log.count("\n"), 1)  # перевод строки из формы не порождает поддельную строку лога
        self.assertIn("contact='@ivan\\n[access-request] fake'", log)
        self.assertIn("ip=203.0.113.7", log)

    def test_request_url_replaces_form_with_link(self):
        app = MagicLink(secret_app, tokens=TOKEN, request_url='mailto:owner@example.com?subject=Доступ"')
        _, _, body, _ = call(app)
        self.assertIn('<a class="btn" href="mailto:owner@example.com?subject=Доступ&quot;">', body)
        self.assertNotIn("<form", body)
        # Встроенный эндпоинт выключен: POST уходит в обычную проверку доступа.
        self.assertEqual(call(app, path="/_access/request", method="POST")[0], "403 Forbidden")


class NamedTokensTest(unittest.TestCase):
    """Токен на каждого человека: отзыв одного не задевает остальных."""

    def test_parse_tokens(self):
        self.assertEqual(parse_tokens(" alice:tok-a, tok-b ,, bob : tok-c "),
                         [("alice", "tok-a"), ("", "tok-b"), ("bob", "tok-c")])
        self.assertEqual(parse_tokens(["x:1", ":2", "y:"]), [("x", "1"), ("", "2")])
        self.assertEqual(parse_tokens(""), [])

    def test_personal_links_and_revocation(self):
        app = MagicLink(secret_app, tokens="alice:tok-a,bob:tok-b", request_url="")
        status, headers, _, log = call(app, query="token=tok-b")
        self.assertEqual(status, "302 Found")
        self.assertIn("вход по ссылке (bob)", log)
        self.assertNotIn("tok-b", log)
        bob_cookie = headers["Set-Cookie"].split(";")[0]
        alice_cookie = "%s=%s" % (COOKIE_NAME, _cookie_value("tok-a"))
        self.assertEqual(call(app, cookie=bob_cookie)[0], "200 OK")

        revoked = MagicLink(secret_app, tokens="alice:tok-a", request_url="")  # bob убран из ACCESS_TOKEN
        self.assertEqual(call(revoked, cookie=bob_cookie)[0], "403 Forbidden")
        self.assertEqual(call(revoked, query="token=tok-b")[0], "403 Forbidden")
        self.assertEqual(call(revoked, cookie=alice_cookie)[0], "200 OK")


async def secret_asgi(scope, receive, send):
    if scope["type"] == "lifespan":
        scope["seen"] = True
        return
    if scope["type"] == "websocket":
        return await send({"type": "websocket.accept"})
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"secret page"})


def asgi_call(app, path="/", query="", cookie=None, method="GET", body=b"", kind="http", headers=None):
    raw_headers = [(b"host", b"app.test")] + ([(b"cookie", cookie.encode())] if cookie else [])
    raw_headers += [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": kind, "method": method, "path": path, "root_path": "", "query_string": query.encode(),
             "headers": raw_headers, "scheme": "http", "client": ("127.0.0.1", 50000)}
    inbox = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(app(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), {})
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode("utf-8")
    return start.get("status"), {k.decode(): v.decode() for k, v in start.get("headers", [])}, body, sent, out.getvalue()


class MagicLinkASGITest(unittest.TestCase):
    def setUp(self):
        self.app = MagicLinkASGI(secret_asgi, tokens=TOKEN, request_url="")
        self.cookie = "%s=%s" % (COOKIE_NAME, _cookie_value(TOKEN))

    def test_no_token_shows_403_with_request_button(self):
        status, headers, body, _, _ = asgi_call(self.app)
        self.assertEqual(status, 403)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn("Запросить доступ", body)
        self.assertNotIn("secret page", body)

    def test_valid_token_sets_cookie_and_redirects(self):
        status, headers, _, _, log = asgi_call(self.app, path="/отчёт", query="token=%s&page=2" % TOKEN,
                                               headers={"x-forwarded-proto": "https"})
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "/%D0%BE%D1%82%D1%87%D1%91%D1%82?page=2")
        self.assertTrue(headers["set-cookie"].startswith(self.cookie + ";"))
        for flag in ("HttpOnly", "SameSite=Lax", "Secure"):
            self.assertIn(flag, headers["set-cookie"])
        self.assertIn("вход по ссылке, ip=127.0.0.1", log)

    def test_cookie_passes_and_bad_credentials_do_not(self):
        self.assertEqual(asgi_call(self.app, cookie=self.cookie)[:3][2], "secret page")
        status, _, body, _, _ = asgi_call(self.app, query="token=wrong")
        self.assertEqual((status, "Ссылка недействительна" in body), (403, True))
        self.assertEqual(asgi_call(self.app, cookie="%s=deadbeef" % COOKIE_NAME)[0], 403)

    def test_no_open_redirect(self):
        _, headers, _, _, _ = asgi_call(self.app, path="//evil.example/x", query="token=" + TOKEN)
        self.assertEqual(headers["location"], "/evil.example/x")

    def test_websocket_requires_cookie(self):
        *_, sent, _ = asgi_call(self.app, kind="websocket")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        *_, sent, _ = asgi_call(self.app, kind="websocket", cookie=self.cookie)
        self.assertEqual(sent, [{"type": "websocket.accept"}])

    def test_lifespan_passes_through(self):
        scope = {"type": "lifespan"}

        async def noop(*_):
            return {}

        asyncio.run(self.app(scope, noop, noop))
        self.assertTrue(scope.get("seen"))

    def test_access_request_is_logged(self):
        status, _, body, _, log = asgi_call(self.app, path="/_access/request", method="POST",
                                            body="contact=%40ivan%0Afake".encode(),
                                            headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
        self.assertEqual(status, 200)
        self.assertIn("Запрос отправлен", body)
        self.assertEqual(log.count("\n"), 1)  # перевод строки из формы экранирован — одна строка лога
        self.assertIn("contact='@ivan\\nfake' ip=203.0.113.7", log)

    def test_named_tokens_and_revocation(self):
        app = MagicLinkASGI(secret_asgi, tokens="alice:tok-a,bob:tok-b", request_url="")
        status, headers, _, _, log = asgi_call(app, query="token=tok-b")
        self.assertEqual(status, 302)
        self.assertIn("(bob)", log)
        bob_cookie = headers["set-cookie"].split(";")[0]
        revoked = MagicLinkASGI(secret_asgi, tokens="alice:tok-a", request_url="")
        self.assertEqual(asgi_call(revoked, cookie=bob_cookie)[0], 403)


if __name__ == "__main__":
    unittest.main()
