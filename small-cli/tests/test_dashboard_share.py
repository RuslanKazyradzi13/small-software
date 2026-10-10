"""Кнопка «Поделиться» в small dashboard: /api/access на настоящем сервере дашборда против мока Railway.

Запуск: cd small-cli && python -m unittest discover -s tests
"""

import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from small_cli import i18n
from small_cli.dashboard import MAX_BODY, SECRET_HEADER, render_page, start_server
from test_small import TOKEN, MockRailway, write

CYRILLIC = re.compile("[Ѐ-ӿ]")
STATE = {"name": "shop", "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1",
         "domain": "shop.up.railway.app"}
OWNER, ALICE = "tok-owner-secret", "tok-alice-secret"  # общая ссылка владельца и личная ссылка alice
MUTATIONS = ("variableCollectionUpsert", "variableDelete", "serviceInstanceRedeploy")


class ShareTestCase(unittest.TestCase):
    def setUp(self):
        i18n.use("en")
        self.addCleanup(i18n.use, "ru")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.app = Path(self.root, "shop")
        tokens = "%s,alice:%s" % (OWNER, ALICE)
        write(self.root, "shop/small.json", json.dumps({"name": "shop", "env": {"ACCESS_TOKEN": tokens}}))
        write(self.root, "shop/.small.json", json.dumps(STATE))
        write(self.root, "shop/magic_link.py", "# supports ACCESS_EMAILS\n")  # шаблон умеет вход по почте
        write(self.root, "draft/small.json", '{"name": "draft"}')            # не задеплоено
        self.railway = MockRailway(service_vars={"ACCESS_TOKEN": tokens, "RAILWAY_PUBLIC_DOMAIN": "x"})
        self.addCleanup(self.railway.close)
        patcher = mock.patch.dict(os.environ, {"SMALL_API_URL": self.railway.url, "RAILWAY_API_TOKEN": TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("RAILWAY_TOKEN", None)  # patch.dict вернёт его на место
        self.server = self.serve()

    def serve(self, token=TOKEN):
        server = start_server(self.root, port=0, token=token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, method, path, payload=None, raw=None, server=None, secret=True, headers=None):
        """Запрос как со страницы дашборда: наш Host и ключ сервера. headers дополняют и заменяют их (None — убрать)."""
        server = server or self.server
        port = server.server_address[1]
        sent = {"Host": "127.0.0.1:%d" % port, SECRET_HEADER: server.secret if secret else None}
        body = raw if raw is not None else None if payload is None else json.dumps(payload).encode("utf-8")
        if body is not None:
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, body=body, headers={k: v for k, v in sent.items() if v is not None})
        res = conn.getresponse()
        res.text = res.read().decode("utf-8", "replace")
        conn.close()
        return res

    def post(self, action, who, app="shop", **kwargs):
        return self.request("POST", "/api/access", {"app": app, "action": action, "who": who}, **kwargs)

    def list(self):
        res = self.request("GET", "/api/access?app=shop")
        self.assertEqual(res.status, 200, res.text)
        return json.loads(res.text)

    def config(self):
        return json.loads((self.app / "small.json").read_text(encoding="utf-8"))["env"]

    def mutations(self):
        return [call for call in self.railway.calls if call in MUTATIONS]


class ShareApiTest(ShareTestCase):
    def test_add_email_updates_config_and_railway_then_redeploys(self):
        res = self.post("add", " Ivan@Company.com ")
        self.assertEqual(res.status, 200, res.text)
        data = json.loads(res.text)
        self.assertEqual((data["kind"], data["who"], data["url"]),
                         ("email", "ivan@company.com", "https://shop.up.railway.app/"))
        self.assertFalse(data["mailConfigured"])  # почта не настроена: страница предупредит, что письма не уходят
        self.assertNotIn("link", data)            # никаких ссылок-паролей: человек входит своей почтой
        env = self.config()
        self.assertEqual(env["ACCESS_EMAILS"], "ivan@company.com")
        self.assertRegex(env["SESSION_SECRET"], r"^[A-Za-z0-9_-]{40,}$")
        self.assertEqual(self.railway.variables["variableCollectionUpsert"]["input"]["variables"],
                         {"ACCESS_EMAILS": "ivan@company.com", "SESSION_SECRET": env["SESSION_SECRET"],
                          "PUBLIC_URL": "https://shop.up.railway.app"})
        self.assertNotIn("PUBLIC_URL", env)  # его small ведёт сам, как в small access add
        self.assertEqual(self.railway.calls[-1], "serviceInstanceRedeploy")
        self.assertNotIn(env["SESSION_SECRET"], res.text)

        res = self.post("add", "@team.com")
        self.assertEqual(res.status, 200, res.text)
        self.assertEqual(json.loads(res.text)["kind"], "domain")
        self.assertEqual(self.config()["ACCESS_EMAILS"], "ivan@company.com, @team.com")
        self.assertEqual(self.config()["SESSION_SECRET"], env["SESSION_SECRET"])  # ключ прежний — входы живы

        res = self.post("add", "IVAN@company.com")
        self.assertEqual(res.status, 400)
        self.assertIn("already on the access list", json.loads(res.text)["error"])

        data = self.list()
        self.assertEqual(data["emails"], [{"who": "ivan@company.com", "domain": False},
                                          {"who": "@team.com", "domain": True}])
        self.assertFalse(data["mailConfigured"])

    def test_configured_mail_means_no_warning(self):
        self.railway.service_vars["RESEND_API_KEY"] = "re_secret_key"
        res = self.post("add", "ivan@company.com")
        self.assertEqual(res.status, 200, res.text)
        self.assertTrue(json.loads(res.text)["mailConfigured"])
        listed = self.request("GET", "/api/access?app=shop")
        self.assertTrue(json.loads(listed.text)["mailConfigured"])
        self.assertNotIn("re_secret_key", res.text + listed.text)

    def test_email_needs_a_template_that_supports_it(self):
        write(self.root, "shop/magic_link.py", "# old version\n")
        res = self.post("add", "ivan@company.com")
        self.assertEqual(res.status, 400)
        self.assertIn("old version", json.loads(res.text)["error"])
        self.assertEqual(self.mutations(), [])

    def test_add_name_returns_the_link_once_and_the_list_never_has_tokens(self):
        res = self.post("add", "bob")
        self.assertEqual(res.status, 200, res.text)
        stored = self.config()["ACCESS_TOKEN"]
        self.assertRegex(stored, r"^%s,alice:%s,bob:[A-Za-z0-9_-]{40,}$" % (OWNER, ALICE))
        token = stored.rsplit("bob:", 1)[1]
        data = json.loads(res.text)
        self.assertEqual((data["kind"], data["who"]), ("link", "bob"))
        self.assertEqual(data["link"], "https://shop.up.railway.app/?token=" + token)
        self.assertEqual(res.text.count(token), 1)
        for secret in (OWNER, ALICE):
            self.assertNotIn(secret, res.text)
        self.assertEqual(self.railway.variables["variableCollectionUpsert"]["input"]["variables"],
                         {"ACCESS_TOKEN": stored})
        self.assertEqual(self.railway.calls[-1], "serviceInstanceRedeploy")

        listed = self.request("GET", "/api/access?app=shop")
        self.assertEqual(listed.status, 200)
        data = json.loads(listed.text)
        self.assertEqual(data["links"], [{"who": "", "shared": True}, {"who": "alice", "shared": False},
                                         {"who": "bob", "shared": False}])
        self.assertEqual(data["emails"], [])
        # Ни список доступа, ни страница, ни список приложений токенов не отдают.
        seen = listed.text + "".join(self.request("GET", path).text for path in ("/", "/api/apps"))
        for secret in (OWNER, ALICE, token):
            self.assertNotIn(secret, seen)

        res = self.post("add", "bob")  # у bob ссылка уже есть: вторую не выдаём и первую не показываем
        self.assertEqual(res.status, 400)
        self.assertIn("already has a link", json.loads(res.text)["error"])
        self.assertNotIn(token, res.text)

    def test_revoke_link_email_and_shared_link(self):
        res = self.post("revoke", "alice")
        self.assertEqual(res.status, 200, res.text)
        self.assertEqual((json.loads(res.text)["kind"], json.loads(res.text)["who"]), ("link", "alice"))
        self.assertEqual(self.config()["ACCESS_TOKEN"], OWNER)
        self.assertEqual(self.railway.service_vars["ACCESS_TOKEN"], OWNER)
        self.assertEqual(self.railway.calls[-1], "serviceInstanceRedeploy")

        self.assertEqual(self.post("add", "ivan@company.com").status, 200)
        res = self.post("revoke", "Ivan@Company.com")
        self.assertEqual(res.status, 200, res.text)
        self.assertEqual(json.loads(res.text)["kind"], "email")
        self.assertNotIn("ACCESS_EMAILS", self.config())
        self.assertNotIn("ACCESS_EMAILS", self.railway.service_vars)

        res = self.post("revoke", "")  # общая ссылка без имени — и последняя
        self.assertEqual(res.status, 200, res.text)
        data = json.loads(res.text)
        self.assertEqual((data["kind"], data["who"]), ("link", ""))
        self.assertIn("last token", " ".join(data["warnings"]))
        self.assertNotIn("ACCESS_TOKEN", self.config())
        self.assertNotIn("ACCESS_TOKEN", self.railway.service_vars)
        self.assertEqual(self.railway.calls[-1], "serviceInstanceRedeploy")
        listed = self.list()
        self.assertEqual((listed["emails"], listed["links"]), ([], []))

        for who in ("alice", "nobody@company.com", ""):  # отзывать уже нечего
            res = self.post("revoke", who)
            self.assertEqual(res.status, 400, who)
            self.assertIn("error", json.loads(res.text))

    def test_invalid_who_changes_nothing(self):
        before = self.config()
        for who in ("bad name", "", "<script>alert(1)</script>", "x" * 41, "a,b", "bob\nevil", "a@b.c" * 70):
            res = self.post("add", who)
            self.assertEqual(res.status, 400, who)
            self.assertIn("specify an email address", json.loads(res.text)["error"])
            self.assertIsNone(CYRILLIC.search(res.text))
        self.assertEqual(self.post("revoke", "bad name").status, 400)
        for payload in ({"app": "shop", "action": "add", "who": 42}, {"app": "shop", "action": "add"},
                        {"app": "shop", "action": "delete", "who": "bob"},
                        {"app": ["shop"], "action": "add", "who": "bob"}, ["shop", "add", "bob"]):
            self.assertEqual(self.request("POST", "/api/access", payload).status, 400, payload)
        self.assertEqual(self.config(), before)
        self.assertEqual(self.mutations(), [])

    def test_unknown_or_undeployed_app(self):
        for app in ("nope", "../shop", "shop/..", "draft", str(self.app), "", "."):
            res = self.post("add", "bob", app=app)
            self.assertEqual(res.status, 404, app)
            self.assertIn("not found", json.loads(res.text)["error"])
            self.assertEqual(self.request("GET", "/api/access?app=" + quote(app)).status, 404, app)
        self.assertEqual(self.request("GET", "/api/access").status, 404)
        self.assertEqual(self.mutations(), [])

    def test_needs_railway_token(self):
        server = self.serve(token=None)
        for res in (self.post("add", "bob", server=server), self.request("GET", "/api/access?app=shop", server=server)):
            self.assertEqual(res.status, 400)
            self.assertIn("RAILWAY_API_TOKEN", json.loads(res.text)["error"])
        self.assertEqual(self.mutations(), [])

    def test_railway_failure_is_reported_and_config_is_kept(self):
        before = self.config()
        with mock.patch.dict(os.environ, {"RAILWAY_API_TOKEN": "wrong-token"}):
            res = self.post("add", "bob")
        self.assertEqual(res.status, 502)
        self.assertIn("Railway: Not Authorized", json.loads(res.text)["error"])
        self.assertEqual(self.config(), before)  # Railway отказал — small.json не тронут
        self.assertEqual(self.mutations(), [])


class ShareSecurityTest(ShareTestCase):
    def assertNothingChanged(self, before):
        self.assertEqual(self.config(), before)
        self.assertEqual(self.mutations(), [])

    def test_missing_or_wrong_secret_is_forbidden(self):
        before = self.config()
        other = self.serve()  # ключ — свой у каждого запуска сервера
        self.assertNotEqual(other.secret, self.server.secret)
        for value in (None, "", "wrong", self.server.secret + "x", self.server.secret[:-1], "é", other.secret):
            headers = {SECRET_HEADER: value}
            res = self.post("add", "bob", headers=headers)
            self.assertEqual(res.status, 403, value)
            self.assertIn("reload the page", json.loads(res.text)["error"])
            self.assertEqual(self.request("GET", "/api/access?app=shop", headers=headers).status, 403, value)
        self.assertNothingChanged(before)
        self.assertEqual(self.railway.calls, [])  # до Railway дело не дошло

    def test_foreign_host_is_forbidden(self):
        before = self.config()
        port = self.server.server_address[1]
        for host in ("evil.example:%d" % port, "127.0.0.1", "127.0.0.1:1", "localhost.evil.example:%d" % port):
            self.assertEqual(self.post("add", "bob", headers={"Host": host}).status, 403, host)
            self.assertEqual(self.request("GET", "/api/access?app=shop", headers={"Host": host}).status, 403, host)
        self.assertNothingChanged(before)
        self.assertEqual(self.post("add", "bob", headers={"Host": "localhost:%d" % port}).status, 200)

    def test_foreign_origin_is_forbidden(self):
        before = self.config()
        port = self.server.server_address[1]
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": "https://127.0.0.1:%d" % port},
                        {"Origin": "http://127.0.0.1:%d.evil.example" % port}, {"Origin": "http://127.0.0.1:1"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            self.assertEqual(self.post("add", "bob", headers=headers).status, 403, headers)
            self.assertEqual(self.request("GET", "/api/access?app=shop", headers=headers).status, 403, headers)
        self.assertNothingChanged(before)
        same = {"Origin": "http://localhost:%d" % port, "Sec-Fetch-Site": "same-origin"}  # как шлёт сам браузер
        self.assertEqual(self.post("add", "bob", headers=same).status, 200)

    def test_cors_preflight_is_never_approved(self):
        res = self.request("OPTIONS", "/api/access", secret=False, headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-small-dashboard"})
        self.assertGreaterEqual(res.status, 400)
        for res in (res, self.post("add", "bob"), self.request("GET", "/api/access?app=shop")):
            self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
            self.assertIsNone(res.getheader("Access-Control-Allow-Headers"))

    def test_only_small_json_bodies(self):
        before = self.config()
        body = json.dumps({"app": "shop", "action": "add", "who": "bob"}).encode("utf-8")
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                             "application/jsonp", None):
            res = self.request("POST", "/api/access", raw=body, headers={"Content-Type": content_type})
            self.assertEqual(res.status, 415, content_type)
        for raw in (b"{oops", b"\xff\xfe", b"", b'"bob"', b"null", b"[]"):
            self.assertEqual(self.request("POST", "/api/access", raw=raw).status, 400, raw)
        for headers, status in (({"Content-Length": str(MAX_BODY + 1)}, 413),
                                ({"Content-Length": "10" * 20}, 413),
                                ({}, 411), ({"Content-Length": "-1"}, 411), ({"Content-Length": "abc"}, 411),
                                ({"Transfer-Encoding": "chunked"}, 411)):
            self.assertEqual(self.headers_only(headers), status, headers)  # тело сервер даже не ждёт
        self.assertNothingChanged(before)
        res = self.request("POST", "/api/access", raw=body, headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(res.status, 200, res.text)
        self.assertEqual(self.request("POST", "/api/apps", raw=body).status, 404)  # других POST-адресов нет

    def headers_only(self, headers):
        """POST без тела с заданными заголовками (Content-Length может врать): код ответа."""
        port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("POST", "/api/access", skip_host=True, skip_accept_encoding=True)
        for name, value in dict({"Host": "127.0.0.1:%d" % port, SECRET_HEADER: self.server.secret,
                                 "Content-Type": "application/json"}, **headers).items():
            conn.putheader(name, value)
        conn.endheaders()
        res = conn.getresponse()
        res.read()
        conn.close()
        return res.status


class SharePageTest(ShareTestCase):
    def test_page_has_share_button_and_window_in_both_languages(self):
        for language, share, add, close in (("en", "Share", "Add", "Close"),
                                            ("ru", "Поделиться", "Добавить", "Закрыть")):
            i18n.use(language)
            res = self.request("GET", "/", secret=False)
            self.assertEqual(res.status, 200)
            page = res.text
            self.assertIn('<html lang="%s">' % language, page)
            self.assertIn('"share": "%s"' % share, page)  # подпись кнопки на карточке — в T
            self.assertIn("openShare(app, share)", page)
            self.assertIn(">%s</button>" % add, page)
            self.assertIn('id="share-close" type="button" aria-label="%s"' % close, page)
            self.assertIn('const SECRET = "%s";' % self.server.secret, page)
            # Tailwind-класс display перебил бы атрибут hidden — окно было бы открыто сразу.
            classes = re.search(r'<div id="share" hidden class="([^"]*)"', page).group(1).split()
            self.assertFalse({"flex", "grid", "block", "inline-flex"} & set(classes), classes)
            self.assertEqual(res.getheader("X-Frame-Options"), "DENY")  # страницу с ключом не встроить в чужой сайт
            self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
            if language == "en":
                self.assertIsNone(CYRILLIC.search(page))

    def test_page_without_server_has_no_secret(self):
        self.assertIn('const SECRET = "";', render_page())


def tearDownModule():
    i18n.use(None)


if __name__ == "__main__":
    unittest.main()
