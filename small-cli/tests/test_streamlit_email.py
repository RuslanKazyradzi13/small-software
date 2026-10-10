"""Вход по рабочей почте в шаблоне streamlit: require_access() в app.py поверх MagicLinkGate из magic_link.py.

Запуск: python -m unittest discover -s tests. Тесты на настоящем Streamlit (streamlit.testing.v1.AppTest) пропускаются,
если он не установлен: pip install "streamlit>=1.40,<2".
"""

import contextlib
import importlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from small_cli import i18n
from small_cli.config import load_config
from small_cli.scaffold import TEMPLATES_DIR
from test_small import TOKEN, MockRailway, run

HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None
needs_streamlit = unittest.skipUnless(HAS_STREAMLIT, "streamlit не установлен")
MAGIC_LINK_DIR = Path(__file__).resolve().parents[2] / "magic-link"  # исходники middleware

EMAILS = "ivan@company.com, @team.com"
SECRET = "streamlit-test-secret"
DOMAIN = "dash.up.railway.app"
STATE = {"name": "dash", "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1", "domain": DOMAIN}
APP_TITLE = "🚀 small + Streamlit"


def token_of(link):
    return parse_qs(urlsplit(link).query)["t"][0]


class StreamlitTemplateTestEmail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = os.path.join(self.tmp.name, "dash")
        code, _, err = run(["init", self.app, "--template", "streamlit"], {})
        self.assertEqual(code, 0, err)
        self.token = load_config(Path(self.app, "small.json"))["env"]["ACCESS_TOKEN"]
        self.environ = {
            "ACCESS_TOKEN": self.token, "ACCESS_EMAILS": EMAILS, "SESSION_SECRET": SECRET, "ACCESS_LANG": "",
            "ACCESS_REQUEST_URL": "", "RAILWAY_PUBLIC_DOMAIN": DOMAIN, "PUBLIC_URL": "", "OWNER_LANG": "en",
            "LOGIN_LINKS_TO_LOG": "", "RESEND_API_KEY": "", "SMTP_URL": "", "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
        }
        self.cookies, self.headers, self.log, self.sent = {}, {}, "", []
        if HAS_STREAMLIT:
            import streamlit as st
            st.cache_resource.clear()  # новый MagicLinkGate: использованные ссылки и лимиты писем — с нуля

    # --- без Streamlit: small init, deploy, access ---

    def test_init_includes_magic_link(self):
        self.assertEqual(sorted(os.listdir(self.app)),
                         [".gitignore", ".railwayignore", "app.py", "magic_link.py", "requirements.txt", "small.json"])
        copy = Path(self.app, "magic_link.py").read_bytes()
        self.assertEqual(copy, (TEMPLATES_DIR / "streamlit" / "magic_link.py").read_bytes())
        if MAGIC_LINK_DIR.is_dir():
            self.assertEqual(copy, (MAGIC_LINK_DIR / "magic_link.py").read_bytes(),
                             "magic_link.py в шаблоне streamlit разошёлся с magic-link/magic_link.py")
        source = Path(self.app, "app.py").read_text(encoding="utf-8")
        compile(source, "app.py", "exec")
        self.assertIn("from magic_link import MagicLinkGate", source)
        self.assertIn("require_access()", source)

    def test_deploy_uploads_magic_link(self):
        server = MockRailway()
        self.addCleanup(server.close)
        code, _, err = run(["deploy", self.app, "--interval", "0"],
                           {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(code, 0, err)
        self.assertEqual(server.uploads,
                         [["./.gitignore", "./.railwayignore", "./app.py", "./magic_link.py", "./requirements.txt"]])

    def access_add(self, who):
        Path(self.app, ".small.json").write_text(json.dumps(STATE), encoding="utf-8")
        server = MockRailway(service_vars={"ACCESS_TOKEN": self.token})
        self.addCleanup(server.close)
        code, out, err = run(["access", "-C", self.app, "add", who], {"SMALL_API_URL": server.url,
                                                                       "RAILWAY_API_TOKEN": TOKEN})
        return server, code, out, err

    def test_access_add_email_works_for_streamlit_app(self):
        server, code, out, err = self.access_add("Ivan@Company.com")
        self.assertEqual(code, 0, err)
        env = load_config(Path(self.app, "small.json"))["env"]
        self.assertEqual(env["ACCESS_EMAILS"], "ivan@company.com")
        self.assertRegex(env["SESSION_SECRET"], r"^[A-Za-z0-9_-]{40,}$")
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["variables"],
                         {"ACCESS_EMAILS": "ivan@company.com", "SESSION_SECRET": env["SESSION_SECRET"],
                          "PUBLIC_URL": "https://" + DOMAIN})
        self.assertEqual(out, "")  # никаких ссылок-паролей: человек входит своей почтой
        self.assertIn("https://%s/" % DOMAIN, err)
        if HAS_STREAMLIT:  # и приложение с этими переменными показывает форму входа по почте
            self.environ.update(ACCESS_EMAILS=env["ACCESS_EMAILS"], SESSION_SECRET=env["SESSION_SECRET"])
            at = self.open()
            self.assertIn("Вход по рабочей почте", at.markdown[0].value)

    def test_old_streamlit_code_is_refused(self):
        app_py = Path(self.app, "app.py")
        new_app = app_py.read_text(encoding="utf-8")
        app_py.write_text("import streamlit as st\n\ndef require_access():\n    pass\n", encoding="utf-8")
        _, code, _, err = self.access_add("ivan@company.com")
        self.assertEqual(code, 1)
        self.assertIn("streamlit", err)
        self.assertIn(str(TEMPLATES_DIR / "streamlit"), err)
        self.assertNotIn("ACCESS_EMAILS", load_config(Path(self.app, "small.json"))["env"])

        app_py.write_text(new_app, encoding="utf-8")
        Path(self.app, "magic_link.py").write_text("# старая версия: только ACCESS_EMAILS для WSGI\n", encoding="utf-8")
        _, code, _, err = self.access_add("ivan@company.com")
        self.assertEqual(code, 1)
        self.assertIn("старой версии", err)
        self.assertIn(os.path.join("streamlit", "magic_link.py"), err)

        os.remove(os.path.join(self.app, "magic_link.py"))
        _, code, _, err = self.access_add("ivan@company.com")
        self.assertEqual(code, 1)
        self.assertIn("magic_link.py", err)
        self.assertNotIn("ACCESS_EMAILS", load_config(Path(self.app, "small.json"))["env"])

    # --- ниже — на настоящем Streamlit ---

    def magic_link(self):
        """Модуль magic_link — тот же объект, которым пользуется app.py (тот же sys.modules)."""
        if "magic_link" not in sys.modules:
            sys.path.insert(0, self.app)
            try:
                importlib.import_module("magic_link")
            finally:
                sys.path.remove(self.app)
        return sys.modules["magic_link"]

    def helper(self, secret=SECRET, emails=EMAILS):
        """MagicLinkGate теста: подписывает ссылки тем же SESSION_SECRET, что у приложения."""
        with contextlib.redirect_stdout(io.StringIO()):
            return self.magic_link().MagicLinkGate(tokens="", request_url="", emails=emails, secret=secret)

    def login_token(self, email="ivan@company.com", **kwargs):
        return token_of(self.helper(**kwargs).login_link(email, "https://" + DOMAIN))

    def session_value(self, email="ivan@company.com", **kwargs):
        helper = self.helper(**kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            return helper.finish_login(token_of(helper.login_link(email, "https://" + DOMAIN)))

    def capture_mail(self):
        """Письма не уходят: _send_login_mail перехвачен, тест читает ссылку из self.sent."""
        patcher = mock.patch.object(self.magic_link(), "_send_login_mail",
                                    side_effect=lambda *args: self.sent.append(args))
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_mail(self, count=1, seconds=5):
        deadline = time.monotonic() + seconds
        while len(self.sent) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.sent[count - 1] if len(self.sent) >= count else None

    @contextlib.contextmanager
    def patched(self):
        """Окружение приложения; куки и заголовки запроса — подменой st.context (AppTest их не задаёт)."""
        from streamlit.runtime.context import ContextProxy
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.environ), \
                mock.patch.object(ContextProxy, "cookies", new_callable=mock.PropertyMock,
                                  return_value=dict(self.cookies)), \
                mock.patch.object(ContextProxy, "headers", new_callable=mock.PropertyMock,
                                  return_value=dict(self.headers)), \
                contextlib.redirect_stdout(out):
            yield out
        self.log += out.getvalue()

    def go(self, runnable):
        """Запуск AppTest (или виджета после click()/input()) в окружении приложения."""
        with self.patched():
            at = runnable.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    def open(self, **query):
        """Открыть приложение в новой вкладке (новая сессия Streamlit) с параметрами адреса."""
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(os.path.join(self.app, "app.py"), default_timeout=30)
        for key, value in query.items():
            at.query_params[key] = value
        return self.go(at)

    @staticmethod
    def frames(at):
        """HTML невидимых iframe, которые пишут куку."""
        return [node.proto.srcdoc for node in at.get("iframe")]

    @staticmethod
    def labels(elements):
        return [element.label for element in elements]

    def assert_app(self, at):
        self.assertEqual([t.value for t in at.title], [APP_TITLE])
        self.assertEqual(self.labels(at.metric), ["Alpha", "Beta", "Gamma"])

    def assert_sign_in_screen(self, at, heading="Вход по рабочей почте"):
        self.assertIn(heading, at.markdown[0].value)
        self.assertEqual(self.labels(at.text_input), ["Рабочая почта"])
        self.assertEqual(self.labels(at.button), ["Получить ссылку для входа"])
        self.assertEqual(len(at.title), 0)  # интерфейс приложения не показан

    def ask(self, email):
        at = self.open()
        at.text_input[0].input(email)
        return self.go(at.button[0].click())

    def sign_in(self, token):
        """Открыть ссылку из письма и нажать «Войти»."""
        at = self.open(t=token)
        self.assertEqual(self.labels(at.button), ["Войти"])
        return self.go(at.button[0].click())

    @needs_streamlit
    def test_email_form_in_visitor_language(self):
        self.assert_sign_in_screen(self.open())
        self.headers = {"Accept-Language": "en-US,en;q=0.9,ru;q=0.8"}
        at = self.open()
        self.assertIn("Sign in with your work email", at.markdown[0].value)
        self.assertEqual((self.labels(at.text_input), self.labels(at.button)),
                         (["Work email"], ["Email me a sign-in link"]))
        self.headers = {}
        self.environ["ACCESS_LANG"] = "en"  # ACCESS_LANG сильнее браузера
        self.assertIn("Sign in with your work email", self.open().markdown[0].value)

    @needs_streamlit
    def test_known_and_unknown_addresses_get_the_same_answer(self):
        self.capture_mail()
        self.headers = {"X-Forwarded-For": "203.0.113.7", "Host": "evil.example"}
        known = self.ask(" Ivan@Company.com ")
        self.assertIn("Проверьте почту", known.markdown[0].value)
        self.assertEqual(self.labels(known.button), ["Ввести другой адрес"])
        email, link, app_name, lang = self.wait_mail()
        self.assertEqual((email, app_name, lang), ("ivan@company.com", DOMAIN, "ru"))
        self.assertTrue(link.startswith("https://%s/?t=" % DOMAIN), link)  # не Host из запроса

        unknown = self.ask("stranger@evil.example")
        self.assertEqual([m.value for m in unknown.markdown], [m.value for m in known.markdown])
        self.assertIn("[access-request] contact='stranger@evil.example' ip=203.0.113.7", self.log)
        time.sleep(0.2)
        self.assertEqual([m[0] for m in self.sent], ["ivan@company.com"])  # незнакомцу письмо не ушло
        self.assertNotIn(link, self.log)  # ссылка для входа в лог не попадает

        again = self.go(known.button[0].click())  # «Ввести другой адрес» — снова форма
        self.assert_sign_in_screen(again)

    @needs_streamlit
    def test_bad_address_and_rate_limit(self):
        self.capture_mail()
        at = self.ask("not-an-email")
        self.assert_sign_in_screen(at)
        self.assertIn("Проверьте адрес", at.warning[0].value)
        for _ in range(3):
            self.assertIn("Проверьте почту", self.ask("ivan@company.com").markdown[0].value)
        at = self.ask("ivan@company.com")
        self.assertIn("Слишком много попыток", at.warning[0].value)
        self.wait_mail(3)
        time.sleep(0.2)
        self.assertEqual([m[0] for m in self.sent], ["ivan@company.com"] * 3)  # 3 письма на адрес за 15 минут

    @needs_streamlit
    def test_link_shows_a_button_and_a_click_signs_in(self):
        token = self.login_token("ivan_petrov@team.com")
        for _ in range(2):  # открытие ссылки (или почтовый сканер) ещё не входит и ссылку не тратит
            at = self.open(t=token)
            self.assertIn("Вход в приложение", at.markdown[0].value)
            self.assertEqual(at.markdown[1].value.replace("\\", ""),
                             "Нажмите кнопку, чтобы войти как ivan_petrov@team.com.")
            self.assertIn("\\_", at.markdown[1].value)  # адрес не превращается в разметку Markdown
            self.assertEqual(self.labels(at.button), ["Войти"])
            self.assertEqual((len(at.title), self.frames(at)), (0, []))

        self.headers = {"X-Forwarded-For": "203.0.113.7"}
        at = self.go(at.button[0].click())
        self.assert_app(at)
        self.assertNotIn("t", at.query_params)  # ссылка убрана из адреса
        self.assertIn("[magic-link] signed in by email (ivan_petrov@team.com), ip=203.0.113.7", self.log)
        [frame] = self.frames(at)
        cookie = re.search(r'"small_access=(e\.[A-Za-z0-9_.-]+); Path=/; Max-Age=2592000; SameSite=Lax"', frame)
        self.assertIsNotNone(cookie, frame)
        self.assertIn("window.parent", frame)
        self.assertIn("'https:' ? '; Secure'", frame)
        self.assertEqual(self.helper().session_owner(cookie.group(1)), "ivan_petrov@team.com")
        self.assertEqual(at.sidebar.caption[0].value.replace("\\", ""), "Вы вошли как ivan_petrov@team.com")
        self.assertEqual(self.labels(at.sidebar.button), ["Выйти"])

        at = self.go(at.sidebar.slider[0].set_value(300))  # дальше сессия держится, кука уже записана
        self.assert_app(at)
        self.assertEqual(self.frames(at), [])

        self.cookies = {"small_access": cookie.group(1)}  # перезагрузка страницы: браузер прислал куку
        self.assert_app(self.open())

    @needs_streamlit
    def test_link_works_only_once(self):
        token = self.login_token()
        first, second = self.open(t=token), self.open(t=token)  # две вкладки с одной ссылкой
        self.assert_app(self.go(first.button[0].click()))
        at = self.go(second.button[0].click())
        self.assert_sign_in_screen(at, "Ссылка для входа устарела")
        self.assertEqual(self.frames(at), [])
        self.assert_sign_in_screen(self.open(t=token), "Ссылка для входа устарела")
        self.assertEqual(self.log.count("signed in by email"), 1)

    @needs_streamlit
    def test_expired_forged_and_foreign_links_are_rejected(self):
        with mock.patch.object(self.magic_link().time, "time", return_value=time.time() - 16 * 60):
            expired = self.login_token()
        token = self.login_token()
        payload, mac = token.split(".")
        forged_payload = self.magic_link()._b64(json.dumps({"e": "boss@company.com", "x": 9999999999,
                                                            "n": "x"}).encode())
        for bad in (expired, payload + "." + mac[::-1], forged_payload + "." + mac, "garbage",
                    self.login_token(secret="another-secret"), self.login_token("boss@evil.example")):
            at = self.open(t=bad)
            self.assert_sign_in_screen(at, "Ссылка для входа устарела")
            self.assertEqual(dict(at.query_params), {})
        self.assertNotIn("signed in", self.log)

    @needs_streamlit
    def test_session_cookie_lets_in_until_revoked(self):
        self.cookies = {"small_access": self.session_value()}
        at = self.open()
        self.assert_app(at)
        self.assertEqual(self.frames(at), [])  # кука уже есть — заново не пишется

        self.environ["ACCESS_EMAILS"] = "@team.com"  # ivan убран из ACCESS_EMAILS
        self.assert_sign_in_screen(self.open())
        self.environ.update(ACCESS_EMAILS=EMAILS, SESSION_SECRET="rotated-secret")  # сменили SESSION_SECRET
        self.assert_sign_in_screen(self.open())

        self.environ["SESSION_SECRET"] = SECRET
        payload, mac = self.cookies["small_access"][2:].split(".")
        forged = self.magic_link()._b64(json.dumps({"e": "boss@company.com", "x": 9999999999}).encode())
        for bad in ("e.%s.%s" % (forged, mac), "e." + self.login_token(), "e." + payload, self.token, ""):
            self.cookies = {"small_access": bad}
            self.assert_sign_in_screen(self.open())

    @needs_streamlit
    def test_sign_out_clears_the_cookie(self):
        self.cookies = {"small_access": self.session_value("olga@team.com")}
        at = self.open()
        self.assert_app(at)
        at = self.go(at.sidebar.button[0].click())
        self.assert_sign_in_screen(at, "Вы вышли")
        [frame] = self.frames(at)
        self.assertIn('"small_access=; Path=/; Max-Age=0; SameSite=Lax"', frame)
        # Браузер прислал куку при открытии страницы, st.context.cookies её ещё помнит — но вход закрыт.
        at = self.go(at)
        self.assert_sign_in_screen(at, "Вы вышли")
        self.assertEqual(self.frames(at), [])

    @needs_streamlit
    def test_personal_token_still_works(self):
        self.headers = {"X-Forwarded-For": "203.0.113.7"}
        self.environ["ACCESS_TOKEN"] = "owner-tok,bob:" + self.token
        at = self.open(token=self.token)
        self.assert_app(at)
        # Как раньше: токен остаётся в адресе (AppTest отдаёт строку, а в Streamlit 1.40 — список).
        self.assertIn(at.query_params["token"], (self.token, [self.token]))
        self.assertEqual((self.labels(at.sidebar.button), self.frames(at)), ([], []))  # без куки и «Выйти»
        self.assertIn("[magic-link] signed in by link (bob), ip=203.0.113.7", self.log)
        self.assert_app(self.go(at))  # перерисовка — без повторной проверки
        self.assertEqual(self.log.count("signed in by link"), 1)

        at = self.open(token="wrong")
        self.assertIn("Ссылка недействительна", at.markdown[0].value)
        self.assertEqual(self.labels(at.button), ["Получить ссылку для входа"])  # и тут же вход по почте

    @needs_streamlit
    def test_without_access_emails_behaves_as_before(self):
        self.environ.update(ACCESS_EMAILS="")
        at = self.open(t=self.login_token())
        self.assertIn("Доступ по приглашению", at.markdown[0].value)
        self.assertEqual(self.labels(at.button), ["Запросить доступ"])
        self.assertEqual(len(at.title), 0)
        self.cookies = {"small_access": self.session_value()}
        self.assertEqual(len(self.open().title), 0)  # без ACCESS_EMAILS сессии по почте не действуют

    @needs_streamlit
    def test_login_links_to_log_mode(self):
        self.environ["LOGIN_LINKS_TO_LOG"] = "1"
        at = self.open()
        at.text_input[0].input("olga@team.com")
        with self.patched() as out:
            at = at.button[0].click().run()
            deadline = time.monotonic() + 5  # письмо «уходит» в фоне — ждём строку лога
            while "sign-in link" not in out.getvalue() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIn("Проверьте почту", at.markdown[0].value)
        found = re.search(r"\[magic-link\] sign-in link \(olga@team\.com\): (https://\S+)", self.log)
        self.assertIsNotNone(found, self.log)
        self.assertTrue(found.group(1).startswith("https://%s/?t=" % DOMAIN))
        self.assert_app(self.sign_in(token_of(found.group(1))))

    @needs_streamlit
    def test_link_address_comes_from_public_url_not_host(self):
        self.capture_mail()
        self.environ["RAILWAY_PUBLIC_DOMAIN"] = ""
        self.headers = {"Host": "evil.example"}
        self.assertIn("Проверьте почту", self.ask("ivan@company.com").markdown[0].value)
        self.assertIn("PUBLIC_URL", self.log)
        time.sleep(0.2)
        self.assertEqual(self.sent, [])
        self.environ["PUBLIC_URL"] = "https://reports.company.com/"
        self.ask("olga@team.com")
        self.assertTrue(self.wait_mail()[1].startswith("https://reports.company.com/?t="))


def setUpModule():
    i18n.use("ru")  # проверки читают русский текст small; язык машины и CI не важен


def tearDownModule():
    i18n.use(None)


if __name__ == "__main__":
    unittest.main()
