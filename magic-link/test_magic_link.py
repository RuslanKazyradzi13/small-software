"""Тесты magic_link.py (WSGI и ASGI). Запуск: python -m unittest test_magic_link"""

import asyncio
import contextlib
import io
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from wsgiref.util import setup_testing_defaults

from urllib.parse import parse_qs, urlencode, urlsplit

import magic_link
from magic_link import (COOKIE_NAME, LINK_TTL, MagicLink, MagicLinkASGI, MagicLinkGate, _cookie_value, parse_emails,
                        parse_tokens)

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
        with mock.patch.dict("os.environ", {"ACCESS_TOKEN": "", "ACCESS_EMAILS": ""}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                app = MagicLink(secret_app)
        self.assertIn("доступ закрыт для всех", out.getvalue())
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


class EmailLoginBase(unittest.TestCase):
    """Вход по почте: письмо перехватывается (magic_link._send_login_mail), ссылка из него открывается тестом."""

    EMAILS = "ivan@company.com, @team.com"

    def setUp(self):
        env = mock.patch.dict(os.environ, {"RAILWAY_PUBLIC_DOMAIN": "reports.up.railway.app", "PUBLIC_URL": "",
                                           "LOGIN_LINKS_TO_LOG": "1", "RESEND_API_KEY": "", "SMTP_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        self.sent, self.langs = [], []
        mailer = mock.patch("magic_link._send_login_mail",
                            side_effect=lambda *args: (self.langs.append(args[3]), self.sent.append(args[:3])))
        mailer.start()
        self.addCleanup(mailer.stop)
        self.app = self.make()

    def make(self, emails=None, secret="test-secret"):
        return MagicLink(secret_app, tokens="owner-token", request_url="", emails=emails or self.EMAILS, secret=secret)

    def ask(self, email, app=None, ip="203.0.113.7", host="reports.up.railway.app", language=None):
        headers = {"HTTP_X_FORWARDED_FOR": ip, "HTTP_HOST": host}
        if language:
            headers["HTTP_ACCEPT_LANGUAGE"] = language
        return call(app or self.app, path="/_access/login", method="POST", body=urlencode({"email": email}).encode(),
                    headers=headers)

    def wait_mail(self, count=1, seconds=3):
        deadline = time.monotonic() + seconds
        while len(self.sent) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.sent[count - 1] if len(self.sent) >= count else None

    @staticmethod
    def token_of(link):
        return parse_qs(urlsplit(link).query)["t"][0]

    def confirm(self, token, app=None):
        return call(app or self.app, path="/_access/verify", method="POST", body=urlencode({"t": token}).encode(),
                    headers={"HTTP_X_FORWARDED_PROTO": "https"})

    def login(self, email="ivan@company.com", app=None):
        before = len(self.sent)
        self.ask(email, app, ip="192.0.2.%d" % before)
        _, link, _ = self.wait_mail(before + 1)
        status, headers, _, _ = self.confirm(self.token_of(link), app)
        self.assertEqual(status, "302 Found")
        return headers["Set-Cookie"].split(";")[0]


class EmailLoginTest(EmailLoginBase):
    def test_parse_emails(self):
        self.assertEqual(parse_emails(" Ivan@Company.com, @Team.COM, мусор, a@b, ,x@y.com "), {"ivan@company.com", "@team.com", "x@y.com"})

    def test_login_page_replaces_request_form(self):
        status, _, body, _ = call(self.app)
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Вход по рабочей почте", body)
        self.assertIn('action="/_access/login"', body)
        self.assertIn('type="email"', body)
        self.assertNotIn("Запросить доступ", body)

    def test_allowed_email_gets_link_and_logs_in(self):
        status, _, body, _ = self.ask(" Ivan@Company.com ")
        self.assertEqual(status, "200 OK")
        self.assertIn("Проверьте почту", body)
        email, link, app_name = self.wait_mail()
        self.assertEqual((email, app_name), ("ivan@company.com", "reports.up.railway.app"))
        self.assertTrue(link.startswith("https://reports.up.railway.app/_access/verify?t="), link)

        # Открытие ссылки (или почтовый сканер) ещё не входит: показывается кнопка.
        query = urlsplit(link).query
        status, headers, body, _ = call(self.app, path="/_access/verify", query=query)
        self.assertEqual(status, "200 OK")
        self.assertIn("войти как ivan@company.com", body)
        self.assertIn('method="post" action="/_access/verify"', body)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

        status, headers, _, log = self.confirm(self.token_of(link))
        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(COOKIE_NAME + "=e."))
        for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Max-Age=2592000"):
            self.assertIn(flag, cookie)
        self.assertIn("вход по почте (ivan@company.com)", log)
        status, _, body, _ = call(self.app, cookie=cookie.split(";")[0])
        self.assertEqual((status, body), ("200 OK", "secret page"))

    def test_link_is_single_use(self):
        self.ask("ivan@company.com")
        token = self.token_of(self.wait_mail()[1])
        for _ in range(2):  # сканер может открыть ссылку несколько раз — она остаётся рабочей
            self.assertEqual(call(self.app, path="/_access/verify", query=urlencode({"t": token}))[0], "200 OK")
        self.assertEqual(self.confirm(token)[0], "302 Found")
        status, _, body, _ = self.confirm(token)
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Ссылка для входа устарела", body)
        self.assertIn('action="/_access/login"', body)  # сразу можно запросить новую

    def test_unknown_email_gets_the_same_answer_and_becomes_a_request(self):
        known = self.ask("ivan@company.com")
        unknown = self.ask("stranger@evil.example", ip="198.51.100.1")
        self.assertEqual(known[:3], unknown[:3])  # по ответу не понять, есть ли адрес в списке
        self.assertIn("[access-request] contact='stranger@evil.example'", unknown[3])
        self.wait_mail()
        time.sleep(0.2)
        self.assertEqual([m[0] for m in self.sent], ["ivan@company.com"])

    def test_domain_allowlist(self):
        self.ask("petr@team.com")
        self.assertEqual(self.wait_mail()[0], "petr@team.com")
        for email in ("petr@team.com.evil.example", "petr@evilteam.com"):
            self.ask(email, ip="198.51.100.%d" % len(email))
        time.sleep(0.2)
        self.assertEqual(len(self.sent), 1)

    def test_revoked_email_loses_its_session_immediately(self):
        cookie = self.login("ivan@company.com")
        team_cookie = self.login("olga@team.com")
        revoked = self.make(emails="@team.com")  # ivan убран из ACCESS_EMAILS, секрет тот же
        self.assertEqual(call(revoked, cookie=cookie)[0], "403 Forbidden")
        self.assertEqual(call(revoked, cookie=team_cookie)[0], "200 OK")

    def test_sessions_cannot_be_forged(self):
        cookie = self.login()
        other_secret = self.make(secret="another-secret")
        self.assertEqual(call(other_secret, cookie=cookie)[0], "403 Forbidden")
        payload, mac = cookie.split("=e.", 1)[1].split(".")
        forged = magic_link._b64(json.dumps({"e": "boss@company.com", "x": 9999999999}).encode())
        self.assertEqual(call(self.app, cookie="%s=e.%s.%s" % (COOKIE_NAME, forged, mac))[0], "403 Forbidden")
        # Ссылку для входа нельзя выдать за сессию: подпись другого назначения.
        self.ask("ivan@company.com", ip="198.51.100.9")
        token = self.token_of(self.wait_mail(2)[1])
        self.assertEqual(call(self.app, cookie="%s=e.%s" % (COOKIE_NAME, token))[0], "403 Forbidden")

    def test_link_expires_after_15_minutes(self):
        self.ask("ivan@company.com")
        token = self.token_of(self.wait_mail()[1])
        later = time.time() + LINK_TTL + 1
        with mock.patch("magic_link.time.time", return_value=later):
            status, _, body, _ = self.confirm(token)
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("устарела", body)

    def test_rate_limit_per_email(self):
        for i in range(3):
            self.assertEqual(self.ask("ivan@company.com", ip="198.51.100.%d" % i)[0], "200 OK")
        status, _, body, _ = self.ask("ivan@company.com", ip="198.51.100.99")
        self.assertEqual(status, "429 Too Many Requests")
        self.assertIn("Слишком много попыток", body)

    def test_bad_email(self):
        status, _, body, _ = self.ask("not-an-email")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("ошибка", body)

    def test_host_header_is_not_trusted_for_links(self):
        with mock.patch.dict(os.environ, {"RAILWAY_PUBLIC_DOMAIN": ""}):
            *_, log = self.ask("ivan@company.com", host="evil.example")
            time.sleep(0.2)
            self.assertEqual(self.sent, [])
            self.assertIn("PUBLIC_URL", log)
            self.ask("ivan@company.com", host="localhost:8080", ip="198.51.100.2")  # локальная разработка
            self.assertTrue(self.wait_mail()[1].startswith("http://localhost:8080/_access/verify?t="))
        with mock.patch.dict(os.environ, {"PUBLIC_URL": "https://reports.company.com/"}):
            self.ask("ivan@company.com", host="evil.example", ip="198.51.100.3")
            self.assertTrue(self.wait_mail(2)[1].startswith("https://reports.company.com/_access/verify?t="))

    def test_logout_clears_cookie(self):
        cookie = self.login()
        status, headers, body, _ = call(self.app, path="/_access/logout", cookie=cookie)
        self.assertEqual(status, "200 OK")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertIn("Вы вышли", body)

    def test_invitation_links_still_work_alongside_email(self):
        status, headers, _, _ = call(self.app, query="token=owner-token")
        self.assertEqual(status, "302 Found")
        self.assertEqual(call(self.app, cookie=headers["Set-Cookie"].split(";")[0])[0], "200 OK")

    def test_without_secret_sessions_still_work_until_restart(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SESSION_SECRET": ""}), contextlib.redirect_stdout(out):
            app = MagicLink(secret_app, tokens="", request_url="", emails="ivan@company.com")
        self.assertIn("SESSION_SECRET не задан", out.getvalue())
        cookie = self.login(app=app)
        self.assertEqual(call(app, cookie=cookie)[0], "200 OK")


class LanguageTest(EmailLoginBase):
    """Страницы и письма — на языке браузера: русским по-русски, остальным по-английски."""

    EN = {"HTTP_ACCEPT_LANGUAGE": "en-US,en;q=0.9,ru;q=0.8"}

    def test_pick_lang(self):
        pick = magic_link.pick_lang
        self.assertEqual(pick(""), "ru")  # браузер ничего не сказал
        self.assertEqual(pick("en-US,en;q=0.9,ru;q=0.8"), "en")
        self.assertEqual(pick("ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"), "ru")
        self.assertEqual(pick("de-DE,de;q=0.9,en;q=0.5"), "en")
        self.assertEqual(pick("fr-FR"), "en")  # ни ru, ни en — английский понятнее
        self.assertEqual(pick("*"), "ru")  # «любой язык» — по умолчанию
        self.assertEqual(pick("en;q=0.5, *;q=0.9"), "ru")
        self.assertEqual(pick("en;q=0.3, ru;q=0.6"), "ru")
        self.assertEqual(pick("ru", "en"), "en")  # ACCESS_LANG сильнее браузера
        self.assertEqual(pick("en", "ru"), "ru")

    def test_english_pages_and_mail(self):
        status, headers, body, _ = call(self.app, headers=self.EN)
        self.assertEqual(status, "403 Forbidden")
        self.assertEqual(headers["Content-Language"], "en")
        self.assertIn('<html lang="en">', body)
        self.assertIn("Sign in with your work email", body)
        self.assertIn("Email me a sign-in link", body)
        self.assertNotIn("Вход", body)

        _, _, body, _ = self.ask("ivan@company.com", language=self.EN["HTTP_ACCEPT_LANGUAGE"])
        self.assertIn("Check your email", body)
        self.assertEqual(self.wait_mail()[0], "ivan@company.com")
        self.assertEqual(self.langs, ["en"])  # письмо — на языке того, кто просил вход
        token = self.token_of(self.sent[0][1])
        _, _, body, _ = call(self.app, path="/_access/verify", query=urlencode({"t": token}), headers=self.EN)
        self.assertIn("sign in as ivan@company.com", body)
        self.assertIn(">Sign in</button>", body)

    def test_access_lang_forces_the_language(self):
        with mock.patch.dict(os.environ, {"ACCESS_LANG": "en"}):
            app = self.make()
        self.assertIn("Sign in with your work email", call(app)[2])
        with mock.patch.dict(os.environ, {"ACCESS_LANG": "ru"}):
            app = self.make()
        self.assertIn("Вход по рабочей почте", call(app, headers=self.EN)[2])

    def test_request_form_in_english(self):
        app = MagicLink(secret_app, tokens=TOKEN, request_url="")
        body = call(app, headers=self.EN)[2]
        self.assertIn("Invitation only", body)
        self.assertIn('placeholder="Email or Telegram to reach you"', body)
        self.assertIn(">Request access</button>", body)
        self.assertIn("Request sent", call(app, path="/_access/request", method="POST", body=b"contact=x",
                                           headers=self.EN)[2])


class EmailLoginASGITest(EmailLoginBase):
    def make(self, emails=None, secret="test-secret"):
        return MagicLinkASGI(secret_asgi, tokens="", request_url="", emails=emails or self.EMAILS, secret=secret)

    def test_full_flow_and_websocket(self):
        status, _, body, _, _ = asgi_call(self.app)
        self.assertEqual(status, 403)
        self.assertIn("Вход по рабочей почте", body)
        status, _, body, _, _ = asgi_call(self.app, path="/_access/login", method="POST",
                                          body=b"email=olga%40team.com", headers={"x-forwarded-for": "203.0.113.7"})
        self.assertEqual(status, 200)
        token = self.token_of(self.wait_mail()[1])
        status, _, body, _, _ = asgi_call(self.app, path="/_access/verify", query=urlencode({"t": token}))
        self.assertEqual(status, 200)
        self.assertIn("olga@team.com", body)
        status, headers, _, _, log = asgi_call(self.app, path="/_access/verify", method="POST",
                                               body=urlencode({"t": token}).encode())
        self.assertEqual((status, headers["location"]), (302, "/"))
        self.assertIn("вход по почте (olga@team.com)", log)
        cookie = headers["set-cookie"].split(";")[0]
        self.assertEqual(asgi_call(self.app, cookie=cookie)[2], "secret page")
        *_, sent, _ = asgi_call(self.app, kind="websocket", cookie=cookie)
        self.assertEqual(sent, [{"type": "websocket.accept"}])
        self.assertEqual(asgi_call(self.app, path="/_access/verify", method="POST",
                                   body=urlencode({"t": token}).encode())[0], 403)


def quiet(function, *args, **kwargs):
    """(результат, вывод в лог) вызова."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = function(*args, **kwargs)
    return result, out.getvalue()


class MagicLinkGateTest(EmailLoginBase):
    """MagicLinkGate — та же защита без middleware (для Streamlit): письма, одноразовые ссылки, сессии, лимиты."""

    HEADERS = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1", "Host": "evil.example", "User-Agent": "agent\n007"}

    def make(self, emails=None, secret="test-secret", tokens="alice:tok-a"):
        return quiet(MagicLinkGate, tokens=tokens, request_url="", emails=emails or self.EMAILS, secret=secret)[0]

    def link(self, email="ivan@company.com", gate=None):
        """Ссылка для входа из письма (почта перехвачена в EmailLoginBase)."""
        before = len(self.sent)
        self.assertEqual(quiet((gate or self.app).start_login, email, dict(self.HEADERS, **{
            "X-Forwarded-For": "198.51.100.%d" % before}))[0], "check")
        return self.wait_mail(before + 1)[1]

    def session(self, email="ivan@company.com", gate=None):
        return quiet((gate or self.app).finish_login, self.token_of(self.link(email, gate)))[0]

    def test_settings(self):
        self.assertEqual(self.app.emails, {"ivan@company.com", "@team.com"})
        self.assertEqual((self.app.cookie_name, self.app.max_age, self.app.request_url), (COOKIE_NAME, 2592000, ""))
        self.assertEqual(self.make(emails=" ").emails, frozenset())

    def test_start_login_sends_a_link_to_the_public_address(self):
        result, log = quiet(self.app.start_login, " Ivan@Company.com ", self.HEADERS)
        self.assertEqual((result, log), ("check", ""))
        email, link, app_name = self.wait_mail()
        self.assertEqual((email, app_name), ("ivan@company.com", "reports.up.railway.app"))
        self.assertTrue(link.startswith("https://reports.up.railway.app/?t="), link)  # Host из запроса не в ходу
        self.assertEqual(self.langs, ["ru"])
        self.app.start_login("ivan@company.com", {"accept-language": "en-US,en;q=0.9", "x-forwarded-for": "192.0.2.1"},
                             path="/report")
        self.assertTrue(self.wait_mail(2)[1].startswith("https://reports.up.railway.app/report?t="))
        self.assertEqual(self.langs, ["ru", "en"])  # письмо — на языке того, кто просил вход
        with mock.patch.dict(os.environ, {"RAILWAY_PUBLIC_DOMAIN": ""}):
            result, log = quiet(self.app.start_login, "ivan@company.com", dict(self.HEADERS, **{
                "X-Forwarded-For": "192.0.2.2"}))
        self.assertEqual(result, "check")
        self.assertIn("PUBLIC_URL", log)  # evil.example из Host не подставляется в ссылку
        time.sleep(0.2)
        self.assertEqual(len(self.sent), 2)

    def test_unknown_address_gets_the_same_answer_and_becomes_a_request(self):
        result, log = quiet(self.app.start_login, "stranger@evil.example", self.HEADERS)
        self.assertEqual(result, "check")
        self.assertEqual(log, "[access-request] contact='stranger@evil.example' ip=203.0.113.7 ua='agent\\n007'\n")
        time.sleep(0.2)
        self.assertEqual(self.sent, [])

    def test_bad_email_and_rate_limits(self):
        for bad in ("not-an-email", "ivan@company.com\n[magic-link] fake", "", None):
            self.assertEqual(self.app.start_login(bad, self.HEADERS), "bad_email", bad)
        for i in range(3):
            self.assertEqual(self.app.start_login("ivan@company.com", {"X-Forwarded-For": "198.51.100.%d" % i}),
                             "check")
        self.assertEqual(self.app.start_login("ivan@company.com", {"X-Forwarded-For": "198.51.100.9"}), "slow")
        for i in range(10):  # с одного IP — не больше 10 писем, на любые адреса
            self.assertEqual(quiet(self.app.start_login, "u%d@team.com" % i, self.HEADERS)[0], "check")
        self.assertEqual(self.app.start_login("olga@team.com", self.HEADERS), "slow")

    def test_link_signs_in_once_and_only_by_finish(self):
        token = self.token_of(self.link())
        for _ in range(2):  # открытие ссылки (или почтовый сканер) её не тратит
            self.assertEqual(self.app.login_email(token), "ivan@company.com")
        session, log = quiet(self.app.finish_login, token, self.HEADERS)
        self.assertTrue(session.startswith("e."), session)
        self.assertEqual(log, "[magic-link] вход по почте (ivan@company.com), ip=203.0.113.7\n")
        self.assertEqual(self.app.session_owner(session), "ivan@company.com")
        self.assertIsNone(self.app.finish_login(token))
        self.assertIsNone(self.app.login_email(token))

    def test_link_is_spent_once_under_concurrency(self):
        token = self.token_of(self.link())
        barrier, results = threading.Barrier(8), []

        def finish():
            barrier.wait()
            results.append(self.app.finish_login(token))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            threads = [threading.Thread(target=finish) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len([r for r in results if r]), 1)
        self.assertEqual(out.getvalue().count("вход по почте"), 1)

    def test_bad_links_are_rejected(self):
        token = self.token_of(self.link())
        with mock.patch("magic_link.time.time", return_value=time.time() + LINK_TTL + 1):
            self.assertIsNone(self.app.login_email(token))
            self.assertIsNone(self.app.finish_login(token))
        payload, mac = token.split(".")
        forged = magic_link._b64(json.dumps({"e": "boss@company.com", "x": 9999999999, "n": "x"}).encode())
        for bad in (payload + "." + mac[:-2] + ("AA" if mac[-2:] != "AA" else "BB"), forged + "." + mac, "", None, 42):
            self.assertIsNone(self.app.login_email(bad), bad)
            self.assertIsNone(self.app.finish_login(bad), bad)
        self.assertIsNone(self.make(secret="another-secret").login_email(token))
        self.assertIsNone(self.make(emails="@team.com").login_email(token))  # адрес убрали из ACCESS_EMAILS
        stranger = self.app.login_link("stranger@evil.example", "https://reports.up.railway.app")
        self.assertIsNone(self.app.login_email(self.token_of(stranger)))  # подписана, но адреса нет в списке
        self.assertEqual(self.app.login_email(token), "ivan@company.com")

    def test_session_owner_and_revocation(self):
        session = self.session()
        team = self.session("olga@team.com")
        self.assertEqual((self.app.session_owner(session), self.app.session_owner(team)),
                         ("ivan@company.com", "olga@team.com"))
        revoked = self.make(emails="@team.com")  # ivan убран из ACCESS_EMAILS, секрет тот же
        self.assertIsNone(revoked.session_owner(session))
        self.assertEqual(revoked.session_owner(team), "olga@team.com")
        self.assertIsNone(self.make(secret="another-secret").session_owner(session))
        payload, mac = session[2:].split(".")
        forged = magic_link._b64(json.dumps({"e": "boss@company.com", "x": 9999999999}).encode())
        login_token = self.token_of(self.link())
        for bad in ("e.%s.%s" % (forged, mac), "e." + login_token, session[:-3], "", None, mock.MagicMock()):
            self.assertIsNone(self.app.session_owner(bad), bad)  # подпись чужого назначения тоже не годится
        with mock.patch("magic_link.time.time", return_value=time.time() + magic_link.COOKIE_MAX_AGE + 1):
            self.assertIsNone(self.app.session_owner(session))  # 30 дней прошли

    def test_sessions_are_shared_with_the_middleware(self):
        session = self.session()
        middleware = super().make()  # MagicLink с тем же SESSION_SECRET
        self.assertEqual(call(middleware, cookie="%s=%s" % (COOKIE_NAME, session))[2], "secret page")
        cookie = self.login(app=middleware)  # и наоборот: кука от MagicLink подходит MagicLinkGate
        self.assertEqual(self.app.session_owner(cookie.split("=", 1)[1]), "ivan@company.com")

    def test_invitation_tokens(self):
        name, log = quiet(self.app.token_login, "tok-a", self.HEADERS)
        self.assertEqual((name, log), ("alice", "[magic-link] вход по ссылке (alice), ip=203.0.113.7\n"))
        self.assertEqual(quiet(self.make(tokens="tok-b").token_login, "tok-b")[0], "")  # общая ссылка, без имени
        for bad in ("wrong", "", None, "tok-a "):
            self.assertEqual(quiet(self.app.token_login, bad), (None, ""), bad)
        self.assertEqual(self.app.session_owner(_cookie_value("tok-a")), "alice")  # кука MagicLink по токену

    def test_texts_and_language(self):
        self.assertEqual(self.app.lang({"Accept-Language": "en-US,en;q=0.9,ru;q=0.8"}), "en")
        self.assertEqual((self.app.lang({}), self.app.lang(None)), ("ru", "ru"))
        with mock.patch.dict(os.environ, {"ACCESS_LANG": "en"}):
            forced = self.make()
        self.assertEqual(forced.lang({"accept-language": "ru"}), "en")
        self.assertEqual(MagicLinkGate.text("check", "en")[:2], ("✉️", "Check your email"))
        self.assertEqual(MagicLinkGate.text("confirm", "ru", "ivan@company.com"),
                         ("\U0001f512", "Вход в приложение", "Нажмите кнопку, чтобы войти как ivan@company.com."))
        self.assertEqual(MagicLinkGate.text("login", "de")[1], "Вход по рабочей почте")
        self.assertEqual((MagicLinkGate.label("sign_in", "en"), MagicLinkGate.label("get_link", "ru")),
                         ("Sign in", "Получить ссылку для входа"))

    def test_request_access_is_one_log_line(self):
        _, log = quiet(self.app.request_access, " me@example.com\n[access-request] fake " + "x" * 300, self.HEADERS)
        self.assertEqual(log.count("\n"), 1)
        self.assertIn("[access-request] contact='me@example.com\\n[access-request] fake xxx", log)
        self.assertIn("' ip=203.0.113.7 ua='agent\\n007'", log)
        self.assertLess(len(log), 300)  # контакт обрезан до 200 символов

    def test_login_link_for_other_delivery(self):
        link = self.app.login_link(" Ivan@Company.com", "https://reports.company.com/", "/")
        self.assertTrue(link.startswith("https://reports.company.com/?t="), link)
        self.assertEqual(self.app.login_email(self.token_of(link)), "ivan@company.com")



class MailServer:
    """Мок Resend API: запоминает POST /emails, отвечает заданным кодом."""

    def __init__(self, status=200):
        self.requests = []
        box = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                box.requests.append((self.path, dict(self.headers), json.loads(body)))
                raw = b'{"id": "msg_1"}' if status == 200 else b'{"message": "The domain is not verified"}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MailDeliveryTest(unittest.TestCase):
    LINK = "https://reports.up.railway.app/_access/verify?t=abc.def"

    def send(self, env):
        out = io.StringIO()
        base = {"LOGIN_LINKS_TO_LOG": "", "RESEND_API_KEY": "", "SMTP_URL": "", "MAIL_FROM": ""}
        with mock.patch.dict(os.environ, dict(base, **env)), contextlib.redirect_stdout(out):
            magic_link._send_login_mail("ivan@company.com", self.LINK, "reports.up.railway.app")
        return out.getvalue()

    def test_resend(self):
        server = MailServer()
        self.addCleanup(server.close)
        log = self.send({"RESEND_API_KEY": "re_test_key", "RESEND_API_URL": server.url,
                         "MAIL_FROM": "Отчёты <noreply@company.com>"})
        path, headers, body = server.requests[0]
        self.assertEqual(path, "/emails")
        self.assertEqual(headers["Authorization"], "Bearer re_test_key")
        self.assertTrue(headers["User-Agent"])
        self.assertEqual((body["to"], body["from"]), (["ivan@company.com"], "Отчёты <noreply@company.com>"))
        self.assertIn("reports.up.railway.app", body["subject"])
        self.assertIn(self.LINK, body["text"])
        self.assertIn("письмо для входа отправлено: ivan@company.com", log)
        self.assertNotIn(self.LINK, log)  # в обычном режиме ссылка в лог не попадает

    def test_resend_error_is_logged_without_the_key(self):
        server = MailServer(status=403)
        self.addCleanup(server.close)
        log = self.send({"RESEND_API_KEY": "re_secret_key", "RESEND_API_URL": server.url})
        self.assertIn("не удалось отправить письмо для входа на ivan@company.com: HTTP 403", log)
        self.assertIn("not verified", log)
        self.assertNotIn("re_secret_key", log)

    def test_smtp(self):
        sessions = []

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                self.calls = [("connect", host, port)]
                sessions.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self):
                self.calls.append(("ehlo",))

            def has_extn(self, name):
                return name == "starttls"

            def starttls(self):
                self.calls.append(("starttls",))

            def login(self, user, password):
                self.calls.append(("login", user, password))

            def send_message(self, message):
                self.calls.append(("send", message["To"], message["From"], message["Subject"],
                                   message.get_body(("plain",)).get_content()))

        with mock.patch("magic_link.smtplib.SMTP", FakeSMTP):
            log = self.send({"SMTP_URL": "smtp://robot%40company.com:p%40ss@mail.company.com:587"})
        calls = sessions[0].calls
        self.assertEqual(calls[0], ("connect", "mail.company.com", 587))
        self.assertIn(("starttls",), calls)
        self.assertIn(("login", "robot@company.com", "p@ss"), calls)
        send = next(c for c in calls if c[0] == "send")
        self.assertEqual(send[1:3], ("ivan@company.com", "robot@company.com"))
        self.assertIn(self.LINK, send[4])
        self.assertIn("письмо для входа отправлено", log)
        self.assertNotIn("p@ss", log)

    def test_english_letter(self):
        server = MailServer()
        self.addCleanup(server.close)
        out = io.StringIO()
        env = {"LOGIN_LINKS_TO_LOG": "", "SMTP_URL": "", "RESEND_API_KEY": "re_test_key", "RESEND_API_URL": server.url}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            magic_link._send_login_mail("ivan@company.com", self.LINK, "reports.up.railway.app", "en")
        body = server.requests[0][2]
        self.assertEqual(body["subject"], "Sign in to reports.up.railway.app")
        self.assertIn("Sign-in link for reports.up.railway.app:\n" + self.LINK, body["text"])
        self.assertIn(">Sign in</a>", body["html"])

    def test_log_mode_and_unconfigured(self):
        self.assertIn("ссылка для входа (ivan@company.com): " + self.LINK, self.send({"LOGIN_LINKS_TO_LOG": "1"}))
        log = self.send({})
        self.assertIn("почта не настроена", log)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"LOGIN_LINKS_TO_LOG": "", "RESEND_API_KEY": "", "SMTP_URL": ""}), \
                contextlib.redirect_stdout(out):
            MagicLink(secret_app, tokens="", emails="ivan@company.com", secret="s")
        self.assertIn("small mail", out.getvalue())


class TelegramInbox:
    """Мок Bot API: запоминает sendMessage."""

    def __init__(self):
        self.messages = []
        inbox = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                inbox.messages.append((self.path, json.loads(body)))
                raw = b'{"ok": true, "result": {}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, count=1, seconds=5):
        deadline = time.monotonic() + seconds
        while len(self.messages) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.messages

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TelegramTest(unittest.TestCase):
    """Запрос с кнопки «Запросить доступ» приходит владельцу в Telegram."""

    def setUp(self):
        self.inbox = TelegramInbox()
        self.addCleanup(self.inbox.close)
        patcher = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42",
                                               "TELEGRAM_API_URL": self.inbox.url})
        patcher.start()
        self.addCleanup(patcher.stop)

    def check_message(self):
        path, body = self.inbox.wait()[0]
        self.assertEqual(path, "/bot123:abc/sendMessage")
        self.assertEqual(body["chat_id"], "42")
        self.assertNotIn("parse_mode", body)  # текст из формы не размечается
        for part in ("shop.up.railway.app", "bob@example.com", "203.0.113.7", "small access add"):
            self.assertIn(part, body["text"])

    def test_wsgi(self):
        app = MagicLink(secret_app, tokens=TOKEN, request_url="")
        status, _, _, _ = call(app, path="/_access/request", method="POST", body=b"contact=bob%40example.com",
                               headers={"HTTP_HOST": "shop.up.railway.app", "HTTP_X_FORWARDED_FOR": "203.0.113.7"})
        self.assertEqual(status, "200 OK")
        self.check_message()

    def test_asgi(self):
        app = MagicLinkASGI(secret_asgi, tokens=TOKEN, request_url="")
        status = asgi_call(app, path="/_access/request", method="POST", body=b"contact=bob%40example.com",
                           headers={"host": "shop.up.railway.app", "x-forwarded-for": "203.0.113.7"})[0]
        self.assertEqual(status, 200)
        self.check_message()

    def test_unknown_email_at_login_reaches_telegram_with_exact_command(self):
        with mock.patch.dict(os.environ, {"LOGIN_LINKS_TO_LOG": "1"}):
            app = MagicLink(secret_app, tokens="", request_url="", emails="ivan@company.com", secret="s")
        call(app, path="/_access/login", method="POST", body=b"email=bob%40example.com",
             headers={"HTTP_HOST": "shop.up.railway.app", "HTTP_X_FORWARDED_FOR": "203.0.113.7"})
        self.check_message()
        self.assertIn("small access add bob@example.com", self.inbox.messages[0][1]["text"])

    def test_gate_request_access(self):
        gate = MagicLinkGate(tokens=TOKEN, request_url="")
        quiet(gate.request_access, "bob@example.com", {"Host": "shop.up.railway.app", "X-Forwarded-For": "203.0.113.7"})
        self.check_message()
        self.assertIn("small access add bob@example.com", self.inbox.messages[0][1]["text"])

    def test_without_telegram_settings_nothing_is_sent(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": ""}):
            app = MagicLink(secret_app, tokens=TOKEN, request_url="")
        call(app, path="/_access/request", method="POST", body=b"contact=x")
        self.assertEqual(self.inbox.wait(seconds=0.5), [])

    def test_owner_language(self):
        """Владелец читает лог и Telegram на своём языке (OWNER_LANG), посетитель — на языке браузера."""
        for owner_lang, words in (("en", ("Access request to", "Grant access: small access add")),
                                  ("ru", ("Запрос доступа к", "Выдать доступ: small access add"))):
            with mock.patch.dict(os.environ, {"OWNER_LANG": owner_lang}):
                app = MagicLink(secret_app, tokens="bob:" + TOKEN, request_url="")
                call(app, path="/_access/request", method="POST", body=b"contact=x",
                     headers={"HTTP_ACCEPT_LANGUAGE": "en-US"})
                log = call(app, query="token=" + TOKEN)[3]
            text = self.inbox.wait()[-1][1]["text"]
            for word in words:
                self.assertIn(word, text)
            self.inbox.messages.clear()
            login = "signed in by link (bob)" if owner_lang == "en" else "вход по ссылке (bob)"
            self.assertIn("[magic-link] " + login, log)

    def test_telegram_down_does_not_break_the_request(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        with mock.patch.dict(os.environ, {"TELEGRAM_API_URL": "http://127.0.0.1:%d" % closed}):
            app = MagicLink(secret_app, tokens=TOKEN, request_url="")
            status, _, body, _ = call(app, path="/_access/request", method="POST", body=b"contact=x")
        self.assertEqual(status, "200 OK")
        self.assertIn("Запрос отправлен", body)


def setUpModule():
    # Проверки выше читают русские строки лога; английские — в test_owner_language.
    os.environ["OWNER_LANG"] = "ru"


def tearDownModule():
    os.environ.pop("OWNER_LANG", None)


if __name__ == "__main__":
    unittest.main()
