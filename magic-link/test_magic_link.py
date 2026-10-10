"""Tests for magic_link.py (WSGI and ASGI). Run: python -m unittest test_magic_link"""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from wsgiref.util import setup_testing_defaults

from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import magic_link
from magic_link import (COOKIE_NAME, LINK_TTL, MagicLink, MagicLinkASGI, MagicLinkGate, _cookie_value, parse_emails,
                        parse_tokens)

TOKEN = "s3cret-token"


def events(log):
    """The JSON lines (sign-ins, refusals, access requests) of a log, as dicts."""
    return [json.loads(line) for line in log.splitlines() if line.startswith("{")]


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
        self.assertEqual(log.count("\n"), 1)  # a newline from the form doesn't produce a forged log line
        [event] = events(log)
        self.assertEqual(event["message"], "[access-request] contact='@ivan\\n[access-request] fake' ip=203.0.113.7 ua=''")
        self.assertEqual((event["level"], event["magic_link"], event["user"], event["ip"]),
                         ("warn", "request", "@ivan\n[access-request] fake", "203.0.113.7"))

    def test_request_url_replaces_form_with_link(self):
        app = MagicLink(secret_app, tokens=TOKEN, request_url='mailto:owner@example.com?subject=Доступ"')
        _, _, body, _ = call(app)
        self.assertIn('<a class="btn" href="mailto:owner@example.com?subject=Доступ&quot;">', body)
        self.assertNotIn("<form", body)
        # The built-in endpoint is off: the POST goes through the normal access check.
        self.assertEqual(call(app, path="/_access/request", method="POST")[0], "403 Forbidden")


class NamedTokensTest(unittest.TestCase):
    """One token per person: revoking one doesn't affect the others."""

    def test_parse_tokens(self):
        self.assertEqual(parse_tokens(" alice:tok-a, tok-b ,, bob : tok-c "),
                         [("alice", "tok-a"), ("", "tok-b"), ("bob", "tok-c")])
        self.assertEqual(parse_tokens(["x:1", ":2", "y:"]), [("x", "1"), ("", "2")])
        self.assertEqual(parse_tokens(""), [])

    def test_personal_links_and_revocation(self):
        app = MagicLink(secret_app, tokens="alice:tok-a,bob:tok-b", request_url="")
        status, headers, _, log = call(app, query="token=tok-b")
        self.assertEqual(status, "302 Found")
        self.assertEqual([(e["message"], e["magic_link"], e["method"], e["user"]) for e in events(log)],
                         [("[magic-link] вход по ссылке: bob, ip —", "sign_in", "link", "bob")])
        self.assertNotIn("tok-b", log)
        bob_cookie = headers["Set-Cookie"].split(";")[0]
        alice_cookie = "%s=%s" % (COOKIE_NAME, _cookie_value("tok-a"))
        self.assertEqual(call(app, cookie=bob_cookie)[0], "200 OK")

        revoked = MagicLink(secret_app, tokens="alice:tok-a", request_url="")  # bob removed from ACCESS_TOKEN
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
        self.assertEqual(events(log)[0]["message"], "[magic-link] вход по ссылке, ip 127.0.0.1")

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
        self.assertEqual(log.count("\n"), 1)  # the newline from the form is escaped: a single log line
        self.assertIn("contact='@ivan\\nfake' ip=203.0.113.7", events(log)[0]["message"])

    def test_named_tokens_and_revocation(self):
        app = MagicLinkASGI(secret_asgi, tokens="alice:tok-a,bob:tok-b", request_url="")
        status, headers, _, _, log = asgi_call(app, query="token=tok-b")
        self.assertEqual(status, 302)
        self.assertEqual(events(log)[0]["user"], "bob")
        bob_cookie = headers["set-cookie"].split(";")[0]
        revoked = MagicLinkASGI(secret_asgi, tokens="alice:tok-a", request_url="")
        self.assertEqual(asgi_call(revoked, cookie=bob_cookie)[0], 403)


class EmailLoginBase(unittest.TestCase):
    """Email sign-in: the email is intercepted (magic_link._send_login_mail) and the test opens its link."""

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

        # Opening the link (or a mail scanner doing so) doesn't sign in yet: a button is shown.
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
        [event] = events(log)
        self.assertEqual((event["message"], event["method"], event["user"]),
                         ("[magic-link] вход по почте: ivan@company.com, ip —", "email", "ivan@company.com"))
        status, _, body, _ = call(self.app, cookie=cookie.split(";")[0])
        self.assertEqual((status, body), ("200 OK", "secret page"))

    def test_link_is_single_use(self):
        self.ask("ivan@company.com")
        token = self.token_of(self.wait_mail()[1])
        for _ in range(2):  # a scanner may open the link several times; it keeps working
            self.assertEqual(call(self.app, path="/_access/verify", query=urlencode({"t": token}))[0], "200 OK")
        self.assertEqual(self.confirm(token)[0], "302 Found")
        status, _, body, _ = self.confirm(token)
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Ссылка для входа устарела", body)
        self.assertIn('action="/_access/login"', body)  # a new one can be requested right away

    def test_unknown_email_gets_the_same_answer_and_becomes_a_request(self):
        known = self.ask("ivan@company.com")
        unknown = self.ask("stranger@evil.example", ip="198.51.100.1")
        self.assertEqual(known[:3], unknown[:3])  # the response doesn't reveal whether the address is on the list
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
        revoked = self.make(emails="@team.com")  # ivan removed from ACCESS_EMAILS, same secret
        self.assertEqual(call(revoked, cookie=cookie)[0], "403 Forbidden")
        self.assertEqual(call(revoked, cookie=team_cookie)[0], "200 OK")

    def test_sessions_cannot_be_forged(self):
        cookie = self.login()
        other_secret = self.make(secret="another-secret")
        self.assertEqual(call(other_secret, cookie=cookie)[0], "403 Forbidden")
        payload, mac = cookie.split("=e.", 1)[1].split(".")
        forged = magic_link._b64(json.dumps({"e": "boss@company.com", "x": 9999999999}).encode())
        self.assertEqual(call(self.app, cookie="%s=e.%s.%s" % (COOKIE_NAME, forged, mac))[0], "403 Forbidden")
        # A sign-in link can't be passed off as a session: its signature has a different purpose.
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
            self.ask("ivan@company.com", host="localhost:8080", ip="198.51.100.2")  # local development
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
    """Pages and emails follow the browser language: Russian for Russian speakers, English for everyone else."""

    EN = {"HTTP_ACCEPT_LANGUAGE": "en-US,en;q=0.9,ru;q=0.8"}

    def test_pick_lang(self):
        pick = magic_link.pick_lang
        self.assertEqual(pick(""), "ru")  # the browser didn't say
        self.assertEqual(pick("en-US,en;q=0.9,ru;q=0.8"), "en")
        self.assertEqual(pick("ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"), "ru")
        self.assertEqual(pick("de-DE,de;q=0.9,en;q=0.5"), "en")
        self.assertEqual(pick("fr-FR"), "en")  # neither ru nor en: English is a safer bet
        self.assertEqual(pick("*"), "ru")  # "any language": the default
        self.assertEqual(pick("en;q=0.5, *;q=0.9"), "ru")
        self.assertEqual(pick("en;q=0.3, ru;q=0.6"), "ru")
        self.assertEqual(pick("ru", "en"), "en")  # ACCESS_LANG beats the browser
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
        self.assertEqual(self.langs, ["en"])  # the email is in the language of whoever asked to sign in
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
        self.assertEqual(events(log)[0]["user"], "olga@team.com")
        cookie = headers["set-cookie"].split(";")[0]
        self.assertEqual(asgi_call(self.app, cookie=cookie)[2], "secret page")
        *_, sent, _ = asgi_call(self.app, kind="websocket", cookie=cookie)
        self.assertEqual(sent, [{"type": "websocket.accept"}])
        self.assertEqual(asgi_call(self.app, path="/_access/verify", method="POST",
                                   body=urlencode({"t": token}).encode())[0], 403)


def quiet(function, *args, **kwargs):
    """(result, log output) of a call."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = function(*args, **kwargs)
    return result, out.getvalue()


class MagicLinkGateTest(EmailLoginBase):
    """MagicLinkGate: the same protection without middleware (for Streamlit): emails, one-time links, sessions, limits."""

    HEADERS = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1", "Host": "evil.example", "User-Agent": "agent\n007"}

    def make(self, emails=None, secret="test-secret", tokens="alice:tok-a"):
        return quiet(MagicLinkGate, tokens=tokens, request_url="", emails=emails or self.EMAILS, secret=secret)[0]

    def link(self, email="ivan@company.com", gate=None):
        """Sign-in link from the email (mail is intercepted in EmailLoginBase)."""
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
        self.assertTrue(link.startswith("https://reports.up.railway.app/?t="), link)  # the request's Host is not used
        self.assertEqual(self.langs, ["ru"])
        self.app.start_login("ivan@company.com", {"accept-language": "en-US,en;q=0.9", "x-forwarded-for": "192.0.2.1"},
                             path="/report")
        self.assertTrue(self.wait_mail(2)[1].startswith("https://reports.up.railway.app/report?t="))
        self.assertEqual(self.langs, ["ru", "en"])  # the email is in the language of whoever asked to sign in
        with mock.patch.dict(os.environ, {"RAILWAY_PUBLIC_DOMAIN": ""}):
            result, log = quiet(self.app.start_login, "ivan@company.com", dict(self.HEADERS, **{
                "X-Forwarded-For": "192.0.2.2"}))
        self.assertEqual(result, "check")
        self.assertIn("PUBLIC_URL", log)  # evil.example from Host doesn't end up in the link
        time.sleep(0.2)
        self.assertEqual(len(self.sent), 2)

    def test_unknown_address_gets_the_same_answer_and_becomes_a_request(self):
        result, log = quiet(self.app.start_login, "stranger@evil.example", self.HEADERS)
        self.assertEqual(result, "check")
        self.assertEqual(log.count("\n"), 1)
        self.assertEqual(events(log)[0]["message"],
                         "[access-request] contact='stranger@evil.example' ip=203.0.113.7 ua='agent\\n007'")
        time.sleep(0.2)
        self.assertEqual(self.sent, [])

    def test_bad_email_and_rate_limits(self):
        for bad in ("not-an-email", "ivan@company.com\n[magic-link] fake", "", None):
            self.assertEqual(self.app.start_login(bad, self.HEADERS), "bad_email", bad)
        for i in range(3):
            self.assertEqual(self.app.start_login("ivan@company.com", {"X-Forwarded-For": "198.51.100.%d" % i}),
                             "check")
        self.assertEqual(self.app.start_login("ivan@company.com", {"X-Forwarded-For": "198.51.100.9"}), "slow")
        for i in range(10):  # at most 10 emails from one IP, whatever the addresses
            self.assertEqual(quiet(self.app.start_login, "u%d@team.com" % i, self.HEADERS)[0], "check")
        self.assertEqual(self.app.start_login("olga@team.com", self.HEADERS), "slow")

    def test_link_signs_in_once_and_only_by_finish(self):
        token = self.token_of(self.link())
        for _ in range(2):  # opening the link (or a mail scanner doing so) doesn't use it up
            self.assertEqual(self.app.login_email(token), "ivan@company.com")
        session, log = quiet(self.app.finish_login, token, self.HEADERS)
        self.assertTrue(session.startswith("e."), session)
        self.assertEqual([(e["message"], e["ua"]) for e in events(log)],
                         [("[magic-link] вход по почте: ivan@company.com, ip 203.0.113.7", "agent\n007")])
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
        self.assertIsNone(self.make(emails="@team.com").login_email(token))  # address removed from ACCESS_EMAILS
        stranger = self.app.login_link("stranger@evil.example", "https://reports.up.railway.app")
        self.assertIsNone(self.app.login_email(self.token_of(stranger)))  # signed, but the address isn't on the list
        self.assertEqual(self.app.login_email(token), "ivan@company.com")

    def test_session_owner_and_revocation(self):
        session = self.session()
        team = self.session("olga@team.com")
        self.assertEqual((self.app.session_owner(session), self.app.session_owner(team)),
                         ("ivan@company.com", "olga@team.com"))
        revoked = self.make(emails="@team.com")  # ivan removed from ACCESS_EMAILS, same secret
        self.assertIsNone(revoked.session_owner(session))
        self.assertEqual(revoked.session_owner(team), "olga@team.com")
        self.assertIsNone(self.make(secret="another-secret").session_owner(session))
        payload, mac = session[2:].split(".")
        forged = magic_link._b64(json.dumps({"e": "boss@company.com", "x": 9999999999}).encode())
        login_token = self.token_of(self.link())
        for bad in ("e.%s.%s" % (forged, mac), "e." + login_token, session[:-3], "", None, mock.MagicMock()):
            self.assertIsNone(self.app.session_owner(bad), bad)  # a signature made for another purpose is rejected too
        with mock.patch("magic_link.time.time", return_value=time.time() + magic_link.COOKIE_MAX_AGE + 1):
            self.assertIsNone(self.app.session_owner(session))  # 30 days have passed

    def test_sessions_are_shared_with_the_middleware(self):
        session = self.session()
        middleware = super().make()  # MagicLink with the same SESSION_SECRET
        self.assertEqual(call(middleware, cookie="%s=%s" % (COOKIE_NAME, session))[2], "secret page")
        cookie = self.login(app=middleware)  # and the other way round: a MagicLink cookie works for MagicLinkGate
        self.assertEqual(self.app.session_owner(cookie.split("=", 1)[1]), "ivan@company.com")

    def test_invitation_tokens(self):
        name, log = quiet(self.app.token_login, "tok-a", self.HEADERS)
        self.assertEqual((name, [e["message"] for e in events(log)]),
                         ("alice", ["[magic-link] вход по ссылке: alice, ip 203.0.113.7"]))
        self.assertEqual(quiet(self.make(tokens="tok-b").token_login, "tok-b")[0], "")  # shared link, no name
        for bad in ("wrong", "", None, "tok-a "):
            self.assertEqual(quiet(self.app.token_login, bad), (None, ""), bad)
        self.assertEqual(self.app.session_owner(_cookie_value("tok-a")), "alice")  # MagicLink token cookie

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
        [event] = events(log)
        self.assertIn("[access-request] contact='me@example.com\\n[access-request] fake xxx", event["message"])
        self.assertIn("' ip=203.0.113.7 ua='agent\\n007'", event["message"])
        self.assertEqual(len(event["user"]), 200)  # the contact is cut to 200 characters

    def test_login_link_for_other_delivery(self):
        link = self.app.login_link(" Ivan@Company.com", "https://reports.company.com/", "/")
        self.assertTrue(link.startswith("https://reports.company.com/?t="), link)
        self.assertEqual(self.app.login_email(self.token_of(link)), "ivan@company.com")



class MailServer:
    """Resend API mock: records POST /emails and replies with the given status."""

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
        self.assertNotIn(self.LINK, log)  # in normal mode the link doesn't get into the log

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
    """Bot API mock: records sendMessage."""

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
    """A request from the "Request access" button reaches the owner in Telegram."""

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
        self.assertNotIn("parse_mode", body)  # text from the form isn't parsed as markup
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
        """The owner reads the log and Telegram in their language (OWNER_LANG); the visitor gets the browser's."""
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
            login = "signed in by link: bob" if owner_lang == "en" else "вход по ссылке: bob"
            self.assertEqual(events(log)[0]["message"], "[magic-link] %s, ip —" % login)

    def test_telegram_down_does_not_break_the_request(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        with mock.patch.dict(os.environ, {"TELEGRAM_API_URL": "http://127.0.0.1:%d" % closed}):
            app = MagicLink(secret_app, tokens=TOKEN, request_url="")
            status, _, body, _ = call(app, path="/_access/request", method="POST", body=b"contact=x")
        self.assertEqual(status, "200 OK")
        self.assertIn("Запрос отправлен", body)



def whoami_app(environ, start_response):
    """An app that shows who magic_link let in and which X-Small-* headers reached it."""
    body = json.dumps({"user": environ.get("magic_link.user"), "method": environ.get("magic_link.method"),
                       "small_user": environ.get("HTTP_X_SMALL_USER")}).encode()
    start_response("200 OK", [("Content-Type", "application/json")])
    return [body]


async def whoami_asgi(scope, receive, send):
    if scope["type"] == "websocket":
        return await send({"type": "websocket.accept", "user": scope.get("magic_link.user")})
    headers = {k.decode(): v.decode() for k, v in scope.get("headers") or []}
    body = json.dumps({"user": scope.get("magic_link.user"), "method": scope.get("magic_link.method"),
                       "small_user": headers.get("x-small-user")}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


class IdentityForTheAppTest(EmailLoginBase):
    """The app learns who is signed in: environ / scope["magic_link.user"] and ["magic_link.method"]."""

    def make(self, emails=None, secret="test-secret"):
        return MagicLink(whoami_app, tokens="bob:tok-b,tok-shared", request_url="", emails=emails or self.EMAILS,
                         secret=secret)

    def test_user_and_method(self):
        email = json.loads(call(self.app, cookie=self.login())[2])
        self.assertEqual((email["user"], email["method"]), ("ivan@company.com", "email"))
        bob = json.loads(call(self.app, cookie="%s=%s" % (COOKIE_NAME, _cookie_value("tok-b")))[2])
        self.assertEqual((bob["user"], bob["method"]), ("bob", "link"))
        shared = json.loads(call(self.app, cookie="%s=%s" % (COOKIE_NAME, _cookie_value("tok-shared")))[2])
        self.assertEqual((shared["user"], shared["method"]), ("", "link"))
        google = self.app.gate.new_session("olga@team.com", "google")
        self.assertEqual(json.loads(call(self.app, cookie="%s=%s" % (COOKIE_NAME, google))[2])["method"], "google")

    def test_nobody_else_can_claim_the_gateway_headers(self):
        cookie = self.login()
        seen = json.loads(call(self.app, cookie=cookie, headers={"HTTP_X_SMALL_USER": "boss@company.com"})[2])
        self.assertEqual((seen["user"], seen["small_user"]), ("ivan@company.com", None))
        app = MagicLinkASGI(whoami_asgi, tokens="", request_url="", emails=self.EMAILS, secret="test-secret")
        _, _, body, _, _ = asgi_call(app, cookie=cookie, headers={"x-small-user": "boss@company.com"})
        self.assertEqual((json.loads(body)["user"], json.loads(body)["small_user"]), ("ivan@company.com", None))


class BehindGatewayTest(unittest.TestCase):
    """SMALL_GATEWAY_KEY: the app is behind the small gateway and trusts only what it signed."""

    KEY = "gateway-key"

    def signed(self, user="ivan@company.com", method="google", key=KEY, now=None):
        return {"HTTP_X_SMALL_USER": user, "HTTP_X_SMALL_METHOD": method,
                "HTTP_X_SMALL_AUTH": magic_link.gateway_auth(key, user, method, now)}

    def test_signature(self):
        auth = magic_link.gateway_auth(self.KEY, "ivan@company.com", "email", now=1000)
        self.assertTrue(auth.startswith("1000."))
        # The same as magic-link.js (its tests hold the same vector).
        self.assertEqual(magic_link.gateway_auth("k", "ivan@company.com", "email", now=1000),
                         "1000.ZrBSpSZFSwHxn5XA3H2cLNHOzS5bnkoWx-3A0rQCdWY")
        self.assertEqual(magic_link.gateway_identity(self.KEY, "ivan@company.com", "email", auth, now=1000),
                         ("ivan@company.com", "email"))
        for args in ((self.KEY, "boss@company.com", "email", auth), (self.KEY, "ivan@company.com", "link", auth),
                     ("another-key", "ivan@company.com", "email", auth), ("", "ivan@company.com", "email", auth),
                     (self.KEY, "ivan@company.com", "email", "x.y"), (self.KEY, "ivan@company.com", "email", "")):
            self.assertIsNone(magic_link.gateway_identity(*args, now=1000), args[1:])
        self.assertIsNone(magic_link.gateway_identity(self.KEY, "ivan@company.com", "email", auth,
                                                      now=1000 + magic_link.GATEWAY_TTL + 1))  # too old
        self.assertIsNone(magic_link.gateway_identity(self.KEY, "ivan@company.com", "email", auth, now=1000 - 3600))

    def test_wsgi(self):
        with mock.patch.dict(os.environ, {"ACCESS_EMAILS": "", "ACCESS_TOKEN": ""}):
            app, log = quiet(MagicLink, whoami_app, gateway_key=self.KEY)
        self.assertIn("за шлюзом small", log)
        self.assertNotIn("доступ закрыт для всех", log)  # no access variables is normal behind the gateway
        status, _, body, _ = call(app, headers=self.signed())
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body), {"user": "ivan@company.com", "method": "google",
                                            "small_user": "ivan@company.com"})
        self.assertEqual(json.loads(call(app, headers=self.signed(user="", method="link"))[2])["user"], "")
        for headers in ({}, self.signed(key="forged"), dict(self.signed(), HTTP_X_SMALL_USER="boss@company.com"),
                        self.signed(now=time.time() - magic_link.GATEWAY_TTL - 60)):
            status, _, body, _ = call(app, headers=headers)
            self.assertEqual(status, "403 Forbidden", headers)
            self.assertIn("Откройте приложение по его адресу", body)
        # Sign-in pages and invite links are the gateway's business, not the app's.
        self.assertEqual(call(app, path="/_access/login", method="POST", body=b"email=ivan%40company.com")[0],
                         "403 Forbidden")
        self.assertEqual(call(app, query="token=anything")[0], "403 Forbidden")

    def test_asgi_and_websocket(self):
        app = quiet(MagicLinkASGI, whoami_asgi, gateway_key=self.KEY)[0]
        headers = {k[5:].lower().replace("_", "-"): v for k, v in self.signed(method="microsoft").items()}
        status, _, body, _, _ = asgi_call(app, headers=headers)
        self.assertEqual((status, json.loads(body)["method"]), (200, "microsoft"))
        self.assertEqual(asgi_call(app)[0], 403)
        *_, sent, _ = asgi_call(app, kind="websocket", headers=headers)
        self.assertEqual(sent, [{"type": "websocket.accept", "user": "ivan@company.com"}])
        *_, sent, _ = asgi_call(app, kind="websocket", cookie="small_access=anything")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])

    def test_gate(self):
        gate = quiet(MagicLinkGate, gateway_key=self.KEY)[0]
        self.assertTrue(gate.behind_gateway)
        headers = {k[5:].replace("_", "-"): v for k, v in self.signed().items()}  # any case works
        self.assertEqual(gate.gateway_user(headers), "ivan@company.com")
        self.assertIsNone(gate.gateway_user({}))
        plain = quiet(MagicLinkGate, emails="ivan@company.com", secret="s", gateway_key="")[0]
        self.assertFalse(plain.behind_gateway)
        self.assertIsNone(plain.gateway_user(headers))  # without a key nothing is trusted

    def test_the_key_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"SMALL_GATEWAY_KEY": self.KEY}):
            app = quiet(MagicLink, whoami_app)[0]
        self.assertEqual(call(app, headers=self.signed())[0], "200 OK")
        self.assertEqual(call(app)[0], "403 Forbidden")


def call_raw(app, path, query="", cookie=None, method="GET", body=b"", quiet_log=True):
    """(status, header list) of a WSGI call: unlike call(), keeps repeated headers such as two Set-Cookie."""
    environ = {"PATH_INFO": path, "QUERY_STRING": query, "REQUEST_METHOD": method, "HTTP_X_FORWARDED_PROTO": "https",
               "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body))}
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    setup_testing_defaults(environ)
    captured = {}

    def start_response(status, headers):
        captured.update(status=status, headers=headers)

    with (contextlib.redirect_stdout(io.StringIO()) if quiet_log else contextlib.nullcontext()):
        b"".join(app(environ, start_response))
    return captured["status"], captured["headers"]


class OidcProvider:
    """Google / Microsoft token endpoint mock: records the code exchange and answers with an ID token made of the
    claims the test sets (unsigned: the app trusts TLS to the token endpoint, as OpenID Connect allows)."""

    def __init__(self):
        self.requests, self.claims, self.status = [], {}, 200
        box = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                form = dict(parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()))
                box.requests.append((self.path, form))
                if box.status == 200:
                    claims = box.claims(form) if callable(box.claims) else box.claims
                    token = ".".join(magic_link._b64(json.dumps(part).encode()) for part in ({"alg": "RS256"}, claims))
                    raw = json.dumps({"access_token": "at", "id_token": token + ".c2ln"}).encode()
                else:
                    raw = json.dumps({"error": "invalid_grant", "error_description": "Bad code"}).encode()
                self.send_response(box.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"


class SsoBase(unittest.TestCase):
    """Single sign-on against OidcProvider: Google and Microsoft are set up, email is not."""

    EMAILS = "ivan@company.com, @team.com, olga.gmail@gmail.com"

    def setUp(self):
        self.provider = OidcProvider()
        self.addCleanup(self.provider.close)
        env = mock.patch.dict(os.environ, {
            "RAILWAY_PUBLIC_DOMAIN": "reports.up.railway.app", "PUBLIC_URL": "", "LOGIN_LINKS_TO_LOG": "",
            "RESEND_API_KEY": "", "SMTP_URL": "", "ACCESS_DB": "", "RAILWAY_VOLUME_MOUNT_PATH": "",
            "SESSIONS_REVOKED": "",
            "GOOGLE_CLIENT_ID": "123-abc.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "GOCSPX-secret",
            "GOOGLE_AUTH_URL": "https://accounts.google.test/auth", "GOOGLE_TOKEN_URL": self.provider.url + "/google",
            "MICROSOFT_CLIENT_ID": "ms-client", "MICROSOFT_CLIENT_SECRET": "ms-secret",
            "MICROSOFT_TENANT_ID": TENANT.upper(), "MICROSOFT_LOGIN_URL": self.provider.url})
        env.start()
        self.addCleanup(env.stop)
        self.app = self.make()

    def make(self, emails=None):
        return quiet(MagicLink, secret_app, tokens="", request_url="", emails=emails or self.EMAILS,
                     secret="test-secret")[0]

    def start(self, provider="google", app=None):
        """GET /_access/sso/<provider>: (query of the authorization URL, state cookie for the callback)."""
        status, headers, _, _ = call(app or self.app, path="/_access/sso/" + provider,
                                     headers={"HTTP_X_FORWARDED_PROTO": "https"})
        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Cache-Control"], "no-store")
        return dict(parse_qsl(urlsplit(headers["Location"]).query)), headers["Set-Cookie"].split(";")[0]

    def google(self, nonce, /, **claims):
        base = {"iss": "https://accounts.google.com", "aud": "123-abc.apps.googleusercontent.com", "sub": "1",
                "exp": int(time.time()) + 3600, "iat": int(time.time()), "nonce": nonce,
                "email": "ivan@company.com", "email_verified": True, "hd": "company.com"}
        return {k: v for k, v in dict(base, **claims).items() if v is not None}

    def microsoft(self, nonce, /, **claims):
        base = {"iss": "https://login.microsoftonline.com/%s/v2.0" % TENANT, "aud": "ms-client", "tid": TENANT,
                "exp": int(time.time()) + 3600, "iat": int(time.time()), "nonce": nonce, "oid": "o1",
                "preferred_username": "Ivan@Company.com"}
        return {k: v for k, v in dict(base, **claims).items() if v is not None}

    def finish(self, query, cookie, app=None, **params):
        values = dict({"code": "4/0Abc", "state": query["state"]}, **params)
        return call(app or self.app, path="/_access/sso/callback", query=urlencode(values), cookie=cookie,
                    headers={"HTTP_X_FORWARDED_PROTO": "https", "HTTP_X_FORWARDED_FOR": "203.0.113.9",
                             "HTTP_USER_AGENT": "Firefox"})

    def sign_in(self, provider="google", app=None, **claims):
        """The whole round trip: (status, headers, body, log) of the callback."""
        query, cookie = self.start(provider, app)
        self.provider.claims = getattr(self, provider)(query["nonce"], **claims)
        return self.finish(query, cookie, app)


class SsoTest(SsoBase):
    def test_sign_in_page_offers_the_providers_and_no_email_form_without_mail(self):
        status, headers, body, _ = call(self.app)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("form-action 'self'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("<script", body)
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Вход рабочим аккаунтом", body)
        self.assertIn('href="/_access/sso/google"', body)
        self.assertIn('href="/_access/sso/microsoft"', body)
        self.assertLess(body.index("Войти через Google"), body.index("Войти через Microsoft"))
        self.assertNotIn('type="email"', body)  # email isn't set up: no form that would send nothing
        with mock.patch.dict(os.environ, {"LOGIN_LINKS_TO_LOG": "1"}):
            body = call(self.make())[2]
        self.assertIn("или по ссылке на почту", body)
        self.assertIn('type="email"', body)
        self.assertIn("Sign in with Google", call(self.app, headers={"HTTP_ACCEPT_LANGUAGE": "en"})[2])

    def test_authorization_request(self):
        query, cookie = self.start()
        self.assertEqual({k: query[k] for k in ("client_id", "redirect_uri", "response_type", "scope",
                                                "code_challenge_method", "prompt")},
                         {"client_id": "123-abc.apps.googleusercontent.com", "response_type": "code",
                          "redirect_uri": "https://reports.up.railway.app/_access/sso/callback",
                          "scope": "openid email", "code_challenge_method": "S256", "prompt": "select_account"})
        self.assertTrue(query["state"].startswith("google.") and len(query["state"]) > 30, query["state"])
        self.assertGreater(len(query["nonce"]), 30)
        self.assertNotIn(query["nonce"], cookie)  # derived from the state, not carried in the cookie
        query2, _ = self.start()
        self.assertNotEqual((query["state"], query["nonce"], query["code_challenge"]),
                            (query2["state"], query2["nonce"], query2["code_challenge"]))
        _, headers, _, _ = call(self.app, path="/_access/sso/google", headers={"HTTP_X_FORWARDED_PROTO": "https"})
        for flag in ("small_access_sso=", "HttpOnly", "SameSite=Lax", "Secure", "Max-Age=600", "Path=/"):
            self.assertIn(flag, headers["Set-Cookie"])
        ms, _ = self.start("microsoft")
        self.assertEqual((ms["client_id"], ms["response_mode"], ms["scope"]), ("ms-client", "query", "openid profile email"))
        self.assertEqual(call(self.app, path="/_access/sso/github")[0], "400 Bad Request")

    def test_google_sign_in(self):
        query, cookie = self.start()
        self.provider.claims = self.google(query["nonce"])
        status, headers, _, log = self.finish(query, cookie)
        self.assertEqual((status, headers["Location"]), ("302 Found", "/"))
        session = headers["Set-Cookie"].split(";")[0]
        self.assertTrue(session.startswith(COOKIE_NAME + "=e."), session)
        self.assertEqual(call(self.app, cookie=session)[2], "secret page")
        # The code exchange: the right endpoint, our secret, the same redirect URI and the PKCE verifier.
        path, form = self.provider.requests[0]
        self.assertEqual(path, "/google")
        self.assertEqual({k: form[k] for k in ("grant_type", "code", "redirect_uri", "client_id", "client_secret")},
                         {"grant_type": "authorization_code", "code": "4/0Abc", "client_id": query["client_id"],
                          "redirect_uri": query["redirect_uri"], "client_secret": "GOCSPX-secret"})
        challenge = magic_link._b64(hashlib.sha256(form["code_verifier"].encode()).digest())
        self.assertEqual(challenge, query["code_challenge"])
        [event] = events(log)
        self.assertEqual({k: event[k] for k in ("message", "magic_link", "method", "user", "ip", "ua")},
                         {"message": "[magic-link] вход через Google: ivan@company.com, ip 203.0.113.9",
                          "magic_link": "sign_in", "method": "google", "user": "ivan@company.com",
                          "ip": "203.0.113.9", "ua": "Firefox"})
        self.assertNotIn("GOCSPX-secret", log)
        self.assertNotIn("4/0Abc", log)

    def test_callback_cookies_and_replay(self):
        query, cookie = self.start()
        self.provider.claims = self.google(query["nonce"])
        callback = "code=c&" + urlencode({"state": query["state"]})
        status, headers = call_raw(self.app, "/_access/sso/callback", callback, cookie)
        cookies = [v for k, v in headers if k == "Set-Cookie"]
        self.assertEqual(status, "302 Found")
        self.assertEqual(cookies[0], "small_access_sso=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure")
        self.assertTrue(cookies[1].startswith(COOKIE_NAME + "=e."), cookies)
        # The same answer again (a stolen callback URL with the cookie): refused, the state is spent.
        self.assertEqual(call_raw(self.app, "/_access/sso/callback", callback, cookie)[0], "400 Bad Request")

    def test_microsoft_sign_in(self):
        status, headers, _, log = self.sign_in("microsoft")
        self.assertEqual(status, "302 Found")
        self.assertEqual(self.app.gate.session_owner(headers["Set-Cookie"].split(";")[0].split("=", 1)[1]),
                         "ivan@company.com")
        self.assertEqual(self.provider.requests[0][0], "/%s/oauth2/v2.0/token" % TENANT)
        self.assertEqual(events(log)[0]["method"], "microsoft")

    def test_whole_domain_and_gmail(self):
        self.assertEqual(self.sign_in(email="olga@team.com", hd="team.com")[0], "302 Found")
        # A Gmail address is Google's own: no hd needed.
        self.assertEqual(self.sign_in(email="olga.gmail@gmail.com", hd=None)[0], "302 Found")

    def test_google_accounts_google_does_not_vouch_for_are_refused(self):
        # A personal Google account registered with a company address: no hd, so it may outlive the mailbox.
        status, headers, body, log = self.sign_in(hd=None)
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Нужен рабочий аккаунт", body)
        self.assertIn("ivan@company.com", body)
        self.assertNotIn(COOKIE_NAME + "=e.", headers.get("Set-Cookie", ""))
        self.assertIn("personal Google account", log)
        self.assertEqual(self.sign_in(email_verified=False)[0], "403 Forbidden")
        self.assertEqual(self.sign_in(email_verified="false")[0], "403 Forbidden")

    def test_microsoft_guests_and_other_tenants_are_refused(self):
        other = "11111111-2222-3333-4444-555555555555"
        status, _, body, _ = self.sign_in("microsoft", idp="https://sts.windows.net/%s/" % other)
        self.assertEqual(status, "403 Forbidden")  # a guest from another tenant
        self.assertIn("Нужен рабочий аккаунт", body)
        self.assertEqual(self.sign_in("microsoft", idp="live.com")[0], "403 Forbidden")
        self.assertEqual(self.sign_in("microsoft", preferred_username="ivan_company.com#EXT#@corp.onmicrosoft.com")[0],
                         "403 Forbidden")
        # A member's idp, if present, names our tenant.
        self.assertEqual(self.sign_in("microsoft", idp="https://sts.windows.net/%s/" % TENANT)[0], "302 Found")
        # Tokens of another tenant: a different issuer or tid.
        self.assertEqual(self.sign_in("microsoft", tid=other)[0], "400 Bad Request")
        self.assertEqual(self.sign_in("microsoft", iss="https://login.microsoftonline.com/%s/v2.0" % other)[0],
                         "400 Bad Request")

    def test_tampered_tokens_are_refused(self):
        now = int(time.time())
        for claims in ({"aud": "another-app"}, {"aud": ["another-app", "123-abc.apps.googleusercontent.com"]},
                       {"exp": now - 3600}, {"exp": None}, {"iat": now + 3600}, {"nonce": "another-nonce"},
                       {"nonce": None}, {"iss": "https://evil.example"}, {"email": None}, {"email": "not-an-email"}):
            status, headers, body, log = self.sign_in(**claims)
            self.assertEqual(status, "400 Bad Request", claims)
            self.assertIn("Не удалось войти", body)
            self.assertNotIn(COOKIE_NAME + "=e.", headers.get("Set-Cookie", ""))
            self.assertIn("[magic-link] вход через провайдера не удался", log)
        # A list audience with us is fine when azp says it was issued to us.
        self.assertEqual(self.sign_in(aud=["123-abc.apps.googleusercontent.com", "x"],
                                      azp="123-abc.apps.googleusercontent.com")[0], "302 Found")

    def test_state_ties_the_answer_to_the_browser(self):
        query, cookie = self.start()
        self.provider.claims = self.google(query["nonce"])
        other_query, other_cookie = self.start()
        self.assertEqual(self.finish(query, other_cookie)[0], "400 Bad Request")  # login CSRF: another browser's state
        self.assertEqual(self.finish(query, None)[0], "400 Bad Request")
        self.assertEqual(self.finish(dict(query, state="google.forged"), cookie)[0], "400 Bad Request")
        self.assertEqual(self.finish(dict(query, state=query["state"].replace("google", "microsoft")), cookie)[0],
                         "400 Bad Request")  # same state, other provider: the nonce won't match
        with mock.patch("magic_link.time.time", return_value=time.time() + 601):
            self.assertEqual(self.finish(query, cookie)[0], "400 Bad Request")  # 10 minutes passed
        self.assertEqual(len(self.provider.requests), 1)  # only the swapped-provider try reached a token endpoint
        self.assertEqual(self.finish(query, cookie)[0], "302 Found")

    def test_provider_errors(self):
        query, cookie = self.start()
        status, _, body, log = self.finish(query, cookie, error="access_denied")
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("access_denied", log)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.finish(query, cookie, code="")[0], "400 Bad Request")
        self.provider.status = 400
        status, _, _, log = self.finish(query, cookie)
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("HTTP 400 invalid_grant", log)
        self.assertNotIn("GOCSPX-secret", log)
        with mock.patch.dict(os.environ, {"GOOGLE_TOKEN_URL": "http://127.0.0.1:9"}):
            app = self.make()
        query, cookie = self.start(app=app)
        self.assertEqual(self.finish(query, cookie, app=app)[0], "400 Bad Request")

    def test_an_address_without_access_becomes_an_access_request(self):
        status, headers, body, log = self.sign_in(email="stranger@evil.example", hd="evil.example")
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Нет доступа", body)
        self.assertIn("stranger@evil.example", body)
        self.assertIn('href="/"', body)  # "Use a different account"
        self.assertEqual([(e["magic_link"], e["user"]) for e in events(log)],
                         [("denied", "stranger@evil.example"), ("request", "stranger@evil.example")])
        self.assertNotIn(COOKIE_NAME + "=e.", headers.get("Set-Cookie", ""))

    def test_sessions_follow_the_access_list(self):
        session = self.sign_in()[1]["Set-Cookie"].split(";")[0]
        self.assertEqual(call(self.make(emails="@team.com"), cookie=session)[0], "403 Forbidden")

    def test_settings(self):
        self.assertEqual(list(self.app.gate.sso), ["google", "microsoft"])
        for broken in ({"MICROSOFT_TENANT_ID": "company.com"}, {"MICROSOFT_TENANT_ID": ""},
                       {"MICROSOFT_CLIENT_SECRET": ""}):
            with mock.patch.dict(os.environ, broken):
                gate, log = quiet(MagicLinkGate, emails=self.EMAILS)
            self.assertEqual(gate.sso, ("google",), broken)
            self.assertIn("MICROSOFT_TENANT_ID", log)
        with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_SECRET": "", "MICROSOFT_CLIENT_ID": ""}):
            gate, log = quiet(MagicLinkGate, emails=self.EMAILS)
        self.assertEqual(gate.sso, ())
        self.assertIn("почта не настроена", log)
        self.assertEqual(quiet(MagicLinkGate, emails="")[0].sso, ())  # no ACCESS_EMAILS: nobody to sign in
        # Without ACCESS_EMAILS the sign-on endpoints are off too.
        app = quiet(MagicLink, secret_app, tokens="tok", request_url="", emails="")[0]
        self.assertEqual(call(app, path="/_access/sso/google")[0], "403 Forbidden")

    def test_unknown_app_address(self):
        with mock.patch.dict(os.environ, {"RAILWAY_PUBLIC_DOMAIN": ""}):
            app = self.make()
            status, _, _, log = call(app, path="/_access/sso/google", headers={"HTTP_HOST": "evil.example"})
            self.assertEqual(status, "500 Internal Server Error")
            self.assertIn("PUBLIC_URL", log)
            status, headers, _, _ = call(app, path="/_access/sso/google", headers={"HTTP_HOST": "localhost:8080"})
        self.assertEqual(status, "302 Found")
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8080%2F_access%2Fsso%2Fcallback", headers["Location"])

    def test_asgi(self):
        app = quiet(MagicLinkASGI, secret_asgi, tokens="", request_url="", emails=self.EMAILS, secret="test-secret")[0]
        status, headers, body, _, _ = asgi_call(app)
        self.assertIn('href="/_access/sso/google"', body)
        status, headers, _, _, _ = asgi_call(app, path="/_access/sso/google")
        query = dict(parse_qsl(urlsplit(headers["location"]).query))
        self.provider.claims = self.google(query["nonce"])
        status, headers, _, _, log = asgi_call(app, path="/_access/sso/callback",
                                               query=urlencode({"code": "c", "state": query["state"]}),
                                               cookie=headers["set-cookie"].split(";")[0])
        self.assertEqual((status, headers["location"]), (302, "/"))
        self.assertEqual(events(log)[0]["user"], "ivan@company.com")
        self.assertEqual(asgi_call(app, cookie=headers["set-cookie"].split(";")[0])[2], "secret page")


class IdentityTest(unittest.TestCase):
    """_identity on its own: which ID tokens give an address."""

    GOOGLE = {"id": "google", "client_id": "g"}
    MICROSOFT = {"id": "microsoft", "client_id": "m", "tenant": TENANT}

    def claims(self, **extra):
        base = {"aud": "g", "exp": 2000, "iat": 1000, "nonce": "n", "iss": "accounts.google.com",
                "email": " Ivan@Company.COM ", "email_verified": "true", "hd": "company.com"}
        return {k: v for k, v in dict(base, **extra).items() if v is not None}

    def test_google(self):
        self.assertEqual(magic_link._identity(self.GOOGLE, self.claims(), "n", now=1500), "ivan@company.com")
        # The clock may be off by a few minutes either way.
        self.assertEqual(magic_link._identity(self.GOOGLE, self.claims(exp=1500 - 200), "n", now=1500), "ivan@company.com")
        with self.assertRaises(magic_link.SsoError) as caught:
            magic_link._identity(self.GOOGLE, self.claims(hd=None), "n", now=1500)
        self.assertEqual((caught.exception.unverified, caught.exception.email), (True, "ivan@company.com"))
        with self.assertRaises(magic_link.SsoError) as caught:
            magic_link._identity(self.GOOGLE, self.claims(exp=1500 - 400), "n", now=1500)
        self.assertFalse(caught.exception.unverified)

    def test_microsoft(self):
        claims = {"aud": "m", "exp": 2000, "nonce": "n", "iss": "https://login.microsoftonline.com/%s/v2.0" % TENANT,
                  "tid": TENANT, "preferred_username": "Ivan@Company.com", "email": "boss@company.com"}
        # The sign-in name (UPN), not the mutable email claim.
        self.assertEqual(magic_link._identity(self.MICROSOFT, claims, "n", now=1500), "ivan@company.com")
        for bad in ({"preferred_username": None}, {"preferred_username": "+15551234567"}, {"tid": None}):
            with self.assertRaises(magic_link.SsoError, msg=bad):
                magic_link._identity(self.MICROSOFT, {k: v for k, v in dict(claims, **bad).items() if v is not None},
                                     "n", now=1500)


class StoreTest(EmailLoginBase):
    """ACCESS_DB: sign-in state in SQLite survives a restart and is shared between worker processes."""

    def setUp(self):
        super().setUp()
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        self.db = os.path.join(folder, "state", "access.db")
        env = mock.patch.dict(os.environ, {"ACCESS_DB": self.db, "SESSIONS_REVOKED": ""})
        env.start()
        self.addCleanup(env.stop)
        self.app = self.make()

    def make(self, emails=None, secret="test-secret"):
        app, log = quiet(super().make, emails, secret)
        self.addCleanup(lambda: app.gate.store.db.close() if app.gate.store.persistent else None)
        return app

    def test_a_used_link_stays_used_after_a_restart(self):
        self.ask("ivan@company.com")
        token = self.token_of(self.wait_mail()[1])
        self.assertEqual(self.confirm(token)[0], "302 Found")
        restarted = self.make()  # the same file: a restart, or another worker process
        self.assertTrue(restarted.gate.store.persistent)
        self.assertEqual(self.confirm(token, restarted)[0], "403 Forbidden")
        self.assertTrue(os.path.isfile(self.db))

    def test_a_link_is_spent_once_across_workers(self):
        self.ask("ivan@company.com")
        token = self.token_of(self.wait_mail()[1])
        workers = [self.make() for _ in range(4)]
        barrier, results = threading.Barrier(4), []

        def finish(app):
            barrier.wait()
            results.append(call_raw(app, "/_access/verify", method="POST", body=urlencode({"t": token}).encode(),
                                    quiet_log=False)[0])

        with contextlib.redirect_stdout(io.StringIO()):
            threads = [threading.Thread(target=finish, args=(app,)) for app in workers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(results), ["302 Found", "403 Forbidden", "403 Forbidden", "403 Forbidden"])

    def test_rate_limits_are_shared(self):
        for i in range(3):
            self.assertEqual(self.ask("ivan@company.com", ip="198.51.100.%d" % i)[0], "200 OK")
        self.assertEqual(self.ask("ivan@company.com", app=self.make(), ip="198.51.100.9")[0], "429 Too Many Requests")

    def test_sign_out_on_all_devices(self):
        laptop, phone = self.login(), self.login()
        olga = self.login("olga@team.com")
        status, headers, body, _ = call(self.app, path="/_access/logout", cookie=laptop)
        self.assertEqual(status, "200 OK")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertIn("Выйти на всех устройствах", body)
        token = re.search(r'name="t" value="([^"]+)"', body).group(1)
        self.assertEqual(call(self.app, cookie=phone)[0], "200 OK")  # an ordinary sign-out ends one browser only

        status, _, body, log = call(self.app, path="/_access/logout", method="POST",
                                    body=urlencode({"t": token}).encode())
        self.assertIn("Вы вышли на всех устройствах", body)
        self.assertIn("ivan@company.com", body)
        self.assertEqual(events(log)[0]["magic_link"], "sign_out_all")
        restarted = self.make()
        for app in (self.app, restarted):
            self.assertEqual(call(app, cookie=phone)[0], "403 Forbidden")
            self.assertEqual(call(app, cookie=olga)[0], "200 OK")
        self.assertEqual(call(restarted, cookie=self.login())[0], "200 OK")  # signing in again works

    def test_sign_out_everywhere_needs_a_genuine_token(self):
        cookie = self.login()
        for bad in ("", "forged", self.token_of(self.app.gate.login_link("https://x/", "ivan@company.com"))):
            status, _, body, _ = call(self.app, path="/_access/logout", method="POST", body=urlencode({"t": bad}).encode())
            self.assertIn("Вы вышли", body)
            self.assertNotIn("на всех устройствах</h1>", body)
        self.assertEqual(call(self.app, cookie=cookie)[0], "200 OK")
        # Not signed in, or signed in by link: no button.
        self.assertNotIn("Выйти на всех устройствах", call(self.app, path="/_access/logout")[2])
        link_cookie = "%s=%s" % (COOKIE_NAME, _cookie_value("owner-token"))
        self.assertNotIn("Выйти на всех устройствах", call(self.app, path="/_access/logout", cookie=link_cookie)[2])

    def test_no_button_when_the_state_is_in_memory(self):
        with mock.patch.dict(os.environ, {"ACCESS_DB": ""}):
            app = self.make()
        self.assertFalse(app.gate.store.persistent)
        cookie = self.login(app=app)
        self.assertNotIn("Выйти на всех устройствах", call(app, path="/_access/logout", cookie=cookie)[2])

    def test_volume_and_broken_paths(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.dict(os.environ, {"ACCESS_DB": "", "RAILWAY_VOLUME_MOUNT_PATH": folder}):
            app, log = quiet(MagicLink, secret_app, emails="ivan@company.com", secret="s")
            app.gate.store.db.close()
        self.assertEqual(app.gate.store.path, os.path.join(folder, ".magic-link.db"))
        self.assertIn("состояние входа хранится в", log)
        with mock.patch.dict(os.environ, {"ACCESS_DB": folder}):  # a folder, not a file: falls back to memory
            app, log = quiet(MagicLink, secret_app, emails="ivan@company.com", secret="s")
        self.assertFalse(app.gate.store.persistent)
        self.assertIn("состояние входа хранится в памяти", log)
        with mock.patch.dict(os.environ, {"ACCESS_DB": "memory", "RAILWAY_VOLUME_MOUNT_PATH": folder}):
            self.assertFalse(quiet(MagicLink, secret_app, emails="x@y.com", secret="s")[0].gate.store.persistent)


class RevokedSessionsTest(EmailLoginBase):
    """SESSIONS_REVOKED: the owner signs people out (small access signout), also from a restored address."""

    def test_parse(self):
        self.assertEqual(magic_link.parse_revoked(" Ivan@Company.com=100, @team.com=5, *=7, junk=1, x@y.com=abc, ,"
                                                  "ivan@company.com=50"),
                         {"ivan@company.com": 100, "@team.com": 5, "*": 7})

    def revoke(self, value):
        with mock.patch.dict(os.environ, {"SESSIONS_REVOKED": value}):
            return self.make()

    def test_address_domain_and_everyone(self):
        ivan, olga = self.login(), self.login("olga@team.com")
        later = int(time.time()) + 5
        cases = (("ivan@company.com=%d" % later, 403, 200), ("@team.com=%d" % later, 200, 403),
                 ("*=%d" % later, 403, 403), ("ivan@company.com=%d" % (later - 3600), 200, 200), ("", 200, 200))
        for value, ivan_status, olga_status in cases:
            app = self.revoke(value)
            self.assertEqual((call(app, cookie=ivan)[0][:3], call(app, cookie=olga)[0][:3]),
                             (str(ivan_status), str(olga_status)), value)
        # Invitation-link cookies are revoked with their token, not here.
        owner = "%s=%s" % (COOKIE_NAME, _cookie_value("owner-token"))
        self.assertEqual(call(self.revoke("*=%d" % later), cookie=owner)[0], "200 OK")

    def test_new_sign_ins_work_after_the_cutoff(self):
        with mock.patch("magic_link.time.time", return_value=time.time() - 60):
            old = self.login()
        app = self.revoke("ivan@company.com=%d" % (int(time.time()) - 30))
        self.assertEqual(call(app, cookie=old)[0], "403 Forbidden")
        self.assertEqual(call(app, cookie=self.login(app=app))[0], "200 OK")

    def test_sessions_from_before_0_8_without_an_issue_time(self):
        gate = self.app.gate
        now = int(time.time())
        legacy = "%s=e.%s" % (COOKIE_NAME, gate.sign("session", {"e": "ivan@company.com", "x": now + gate.max_age - 100}))
        self.assertEqual(call(self.app, cookie=legacy)[0], "200 OK")
        self.assertEqual(call(self.revoke("ivan@company.com=%d" % (now - 50)), cookie=legacy)[0], "403 Forbidden")
        self.assertEqual(call(self.revoke("ivan@company.com=%d" % (now - 200)), cookie=legacy)[0], "200 OK")


class GateSsoTest(SsoBase):
    """MagicLinkGate (Streamlit): single sign-on and signing out everywhere without middleware."""

    HEADERS = {"X-Forwarded-For": "203.0.113.9", "Host": "evil.example", "User-Agent": "Safari", "X-Forwarded-Proto": "https"}

    def make(self, emails=None):
        return quiet(MagicLinkGate, tokens="", request_url="", emails=emails or self.EMAILS, secret="test-secret")[0]

    def test_links_and_finish(self):
        self.assertEqual((self.app.sso, self.app.email_form), (("google", "microsoft"), False))
        links, name, value = self.app.sso_links(self.HEADERS)
        cookie = {name: value, "small_access_sso": "forged"}
        self.assertTrue(name.startswith("small_access_sso_"), name)
        self.assertNotEqual(self.app.sso_links(self.HEADERS)[1], name)  # another tab gets its own cookie
        google = dict(parse_qsl(urlsplit(links["google"]).query))
        microsoft = dict(parse_qsl(urlsplit(links["microsoft"]).query))
        self.assertEqual(google["redirect_uri"], "https://reports.up.railway.app/")  # the Streamlit page itself
        # One cookie serves both buttons; each provider gets its own state, nonce and verifier.
        self.assertEqual(google["state"].split(".")[1], microsoft["state"].split(".")[1])
        self.assertNotEqual(google["nonce"], microsoft["nonce"])
        self.assertTrue(self.app.is_sso_return({"state": google["state"], "code": "c"}))
        for other in ({"state": "abc"}, {"state": "google."}, {}, None, {"t": "x"}):
            self.assertFalse(self.app.is_sso_return(other), other)

        self.provider.claims = self.microsoft(microsoft["nonce"])
        (kind, session, email), log = quiet(self.app.sso_finish, {"code": "c", "state": microsoft["state"]}, cookie,
                                            self.HEADERS)
        self.assertEqual((kind, email), ("session", "ivan@company.com"))
        self.assertEqual(self.app.session_owner(session), "ivan@company.com")
        self.assertEqual(events(log)[0]["ua"], "Safari")
        self.assertEqual(self.provider.requests[0][1]["redirect_uri"], "https://reports.up.railway.app/")

        self.provider.claims = self.google(google["nonce"], email="stranger@evil.example", hd="evil.example")
        (kind, session, email), _ = quiet(self.app.sso_finish, {"code": "c", "state": google["state"]}, cookie,
                                          self.HEADERS)
        self.assertEqual((kind, session, email), ("denied", None, "stranger@evil.example"))
        for cookies in ({name: "forged"}, {}, None, {"small_access_sso": value}):
            self.assertEqual(quiet(self.app.sso_finish, {"code": "c", "state": google["state"]}, cookies,
                                   self.HEADERS)[0], ("sso_failed", None, ""), cookies)

    def test_without_sign_on(self):
        with mock.patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "", "MICROSOFT_CLIENT_ID": ""}):
            gate = self.make()
        self.assertIsNone(gate.sso_links(self.HEADERS))
        self.assertTrue(gate.email_form)
        with mock.patch.dict(os.environ, {"RAILWAY_PUBLIC_DOMAIN": ""}):
            self.assertIsNone(self.app.sso_links(self.HEADERS))  # unknown address: no redirect URI to give

    def test_sign_out_everywhere(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with mock.patch.dict(os.environ, {"ACCESS_DB": os.path.join(folder, "a.db")}):
            gate = self.make()
        self.addCleanup(gate._gate.store.db.close)
        self.assertTrue(gate.persistent)
        links, name, value = gate.sso_links(self.HEADERS)
        cookie = {name: value}
        state = dict(parse_qsl(urlsplit(links["google"]).query))
        self.provider.claims = self.google(state["nonce"])
        session = quiet(gate.sso_finish, {"code": "c", "state": state["state"]}, cookie, self.HEADERS)[0][1]
        token = gate.everywhere_token(session)
        self.assertIsNone(gate.everywhere_token("e.forged"))
        self.assertIsNone(self.app.everywhere_token(session))  # self.app keeps its state in memory
        self.assertEqual(quiet(gate.sign_out_everywhere, token, self.HEADERS)[0], "ivan@company.com")
        self.assertIsNone(gate.session_owner(session))
        self.assertIsNone(quiet(gate.sign_out_everywhere, "forged")[0])


def setUpModule():
    # The checks above read Russian log lines; the English ones are in test_owner_language.
    os.environ["OWNER_LANG"] = "ru"


def tearDownModule():
    os.environ.pop("OWNER_LANG", None)


if __name__ == "__main__":
    unittest.main()
