"""Тесты magic_link.py. Запуск: python -m unittest test_magic_link"""

import contextlib
import io
import unittest
from unittest import mock
from wsgiref.util import setup_testing_defaults

from magic_link import COOKIE_NAME, MagicLink, _cookie_value

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


if __name__ == "__main__":
    unittest.main()
