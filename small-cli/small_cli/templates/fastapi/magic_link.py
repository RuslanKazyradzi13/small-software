"""Защита веб-приложения: вход по рабочей почте или по ссылке-приглашению. Один файл, без зависимостей, Python 3.8+.

    from magic_link import MagicLink, MagicLinkASGI
    application = MagicLink(application)        # WSGI: Django; Flask — app.wsgi_app = MagicLink(app.wsgi_app)
    app.add_middleware(MagicLinkASGI)           # ASGI: FastAPI, Starlette; или app = MagicLinkASGI(app)
    gate = MagicLinkGate()                      # без middleware (Streamlit и т. п.): см. класс MagicLinkGate

Два способа пустить человека:
  * Вход по почте — как доступ к Google Doc. Владелец перечисляет адреса в ACCESS_EMAILS. Человек открывает
    приложение, вводит свою почту и получает письмо со ссылкой для входа (15 минут, один раз). Войти может
    только тот, кто читает эту почту; браузер помнит вход 30 дней. Незнакомый адрес — это запрос доступа.
  * Ссылка-приглашение https://<app>/?token=<токен> (ACCESS_TOKEN): middleware ставит куку и перенаправляет
    на тот же адрес без токена. Ссылка работает как пароль: войдёт любой, кому её переслали.
Без доступа — страница 403: форма входа по почте (если задан ACCESS_EMAILS) или кнопка «Запросить доступ».
Страницы и письма — на русском или английском, по языку браузера.

Переменные окружения:
    ACCESS_EMAILS       адреса через запятую; "@company.com" — весь домен. Убрали адрес — вход перестаёт
                        работать сразу, в том числе по уже выданной куке.
    SESSION_SECRET      ключ подписи ссылок для входа и сессий (small создаёт его сам). Не задан — случайный
                        на каждый запуск: после перезапуска всем придётся войти заново.
    RESEND_API_KEY      отправлять письма через Resend (HTTPS — работает и на Railway Hobby);
    MAIL_FROM           отправитель, например "Reports <noreply@company.com>" (домен подтверждён в Resend)
    SMTP_URL            smtp://логин:пароль@сервер:587 или smtps://...:465 (на Railway SMTP — только с тарифа Pro)
    LOGIN_LINKS_TO_LOG  1 — не слать письма, а писать ссылку для входа в лог. Только для проверки.
    PUBLIC_URL          адрес приложения для ссылок в письмах (например, свой домен). Не задан — берётся
                        RAILWAY_PUBLIC_DOMAIN; локально — localhost.
    ACCESS_LANG         язык страниц и писем: ru, en или auto (по умолчанию — по заголовку Accept-Language)
    OWNER_LANG          язык сообщений для владельца — строк лога и уведомлений в Telegram: en (по умолчанию) или ru.
                        small deploy задаёт его по языку small.
    ACCESS_TOKEN        токены ссылок-приглашений через запятую, можно с именами: "alice:токен1,bob:токен2".
                        Убрали чей-то токен — его ссылка и выданные по ней куки перестают работать.
    ACCESS_REQUEST_URL  куда ведёт кнопка «Запросить доступ» (mailto:..., https://t.me/...), если нет входа по
                        почте. Не задан — запрос печатается в лог строкой [access-request]
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                        запросы доступа приходят владельцу в Telegram (подключить: small notify telegram)
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import smtplib
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from http.cookies import CookieError, SimpleCookie
from string import Template
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

COOKIE_NAME = "small_access"
COOKIE_MAX_AGE = 30 * 24 * 3600
LINK_TTL = 15 * 60
REQUEST_PATH = "/_access/request"
LOGIN_PATH = "/_access/login"
VERIFY_PATH = "/_access/verify"
LOGOUT_PATH = "/_access/logout"
# Сколько писем для входа можно запросить за 15 минут: на один адрес и с одного IP.
LIMIT_PER_EMAIL, LIMIT_PER_IP, LIMIT_WINDOW = 3, 10, 15 * 60

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
DOMAIN_RE = re.compile(r"^@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
STATUS = {200: "200 OK", 302: "302 Found", 400: "400 Bad Request", 403: "403 Forbidden", 429: "429 Too Many Requests"}
LANGS = ("ru", "en")
DEFAULT_LANG = "ru"  # если браузер не сказал, какой язык предпочитает

MESSAGES = {
    "ru": {
        "forbidden": ("&#128274;", "Доступ по приглашению",
                      "Эта страница открывается только по ссылке-приглашению от владельца."),
        "invalid": ("&#128274;", "Ссылка недействительна",
                    "Ссылка устарела или скопирована не полностью. Запросите новую."),
        "sent": ("&#9989;", "Запрос отправлен", "Владелец увидит запрос и пришлёт вам ссылку-приглашение."),
        "login": ("&#128274;", "Вход по рабочей почте",
                  "Приложение открывается только для приглашённых. Введите рабочую почту — пришлём ссылку для входа."),
        "check": ("&#9993;&#65039;", "Проверьте почту",
                  "Если у адреса есть доступ, ссылка для входа уже в почте: она действует 15 минут. "
                  "Если доступа нет, владелец получит ваш запрос."),
        "bad_email": ("&#9888;&#65039;", "Проверьте адрес", "Похоже, в адресе почты ошибка. Введите его ещё раз."),
        "slow": ("&#8987;", "Слишком много попыток", "Подождите 15 минут и попробуйте снова."),
        "expired": ("&#8987;", "Ссылка для входа устарела",
                    "Ссылка действует 15 минут и только один раз. Введите почту — пришлём новую."),
        "confirm": ("&#128274;", "Вход в приложение", "Нажмите кнопку, чтобы войти как %s."),
        "logout": ("&#128075;", "Вы вышли", "Чтобы войти снова, введите рабочую почту."),
    },
    "en": {
        "forbidden": ("&#128274;", "Invitation only", "This page opens only with an invitation link from its owner."),
        "invalid": ("&#128274;", "Invalid link", "The link has expired or was copied incompletely. Ask for a new one."),
        "sent": ("&#9989;", "Request sent", "The owner will see your request and send you an invitation link."),
        "login": ("&#128274;", "Sign in with your work email",
                  "This app is for invited people only. Enter your work email and we'll send you a sign-in link."),
        "check": ("&#9993;&#65039;", "Check your email",
                  "If this address has access, a sign-in link is already in your inbox. It works for 15 minutes. "
                  "If it doesn't, the owner will get your request."),
        "bad_email": ("&#9888;&#65039;", "Check the address", "This email address looks wrong. Please enter it again."),
        "slow": ("&#8987;", "Too many attempts", "Please wait 15 minutes and try again."),
        "expired": ("&#8987;", "Sign-in link expired",
                    "The link works for 15 minutes and only once. Enter your email to get a new one."),
        "confirm": ("&#128274;", "Sign in", "Click the button to sign in as %s."),
        "logout": ("&#128075;", "Signed out", "To sign in again, enter your work email."),
    },
}

UI = {
    "ru": {"request": "Запросить доступ", "contact": "Email или Telegram для ответа",
           "get_link": "Получить ссылку для входа", "sign_in": "Войти", "other": "Ввести другой адрес",
           "subject": "Вход в %s", "mail_link": "Войти",
           "mail_text": "Ссылка для входа в %s:\n%s\n\nОна действует 15 минут и только один раз. "
                        "Если вы не запрашивали вход, просто удалите это письмо.\n",
           "mail_intro": "Ссылка для входа в <b>%s</b>:",
           "mail_note": "Она действует 15 минут и только один раз. Если вы не запрашивали вход, просто удалите это письмо."},
    "en": {"request": "Request access", "contact": "Email or Telegram to reach you",
           "get_link": "Email me a sign-in link", "sign_in": "Sign in", "other": "Use a different address",
           "subject": "Sign in to %s", "mail_link": "Sign in",
           "mail_text": "Sign-in link for %s:\n%s\n\nIt works for 15 minutes and only once. "
                        "If you didn't ask to sign in, just delete this email.\n",
           "mail_intro": "Sign-in link for <b>%s</b>:",
           "mail_note": "It works for 15 minutes and only once. If you didn't ask to sign in, just delete this email."},
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


def parse_emails(value):
    """"Ivan@Company.com, @team.com, мусор" -> {"ivan@company.com", "@team.com"}: адреса и домены, без регистра."""
    items = value.split(",") if isinstance(value, str) else value
    found = set()
    for item in items:
        item = item.strip().lower()
        if EMAIL_RE.match(item) or DOMAIN_RE.match(item):
            found.add(item)
    return found


def pick_lang(accept_language, setting="auto"):
    """Язык страницы: ACCESS_LANG (ru/en), иначе самый предпочтительный из ru/en в Accept-Language."""
    if setting in LANGS:
        return setting
    if not (accept_language or "").strip():
        return DEFAULT_LANG
    best, best_q = "en", -1.0  # браузер просит другой язык (de, fr…) — английский понятнее, чем русский
    for part in (accept_language or "").split(","):
        tag, _, params = part.strip().partition(";")
        base = tag.strip().lower().split("-")[0]
        if base == "*":  # «любой язык» (так, например, пишет fetch в Node)
            base = DEFAULT_LANG
        try:
            q = float(params.strip()[2:]) if params.strip().startswith("q=") else 1.0
        except ValueError:
            q = 0.0
        if base in LANGS and q > best_q:
            best, best_q = base, q
    return best


class _Gate:
    """Общая логика WSGI- и ASGI-версий: кто может войти, куки, письма, страницы, логи."""

    def __init__(self, tokens, request_url, cookie_name, max_age, emails=None, secret=None):
        self.entries = parse_tokens(os.environ.get("ACCESS_TOKEN", "") if tokens is None else tokens)
        self.cookies = [(name, _cookie_value(token)) for name, token in self.entries]
        self.emails = parse_emails(os.environ.get("ACCESS_EMAILS", "") if emails is None else emails)
        secret = os.environ.get("SESSION_SECRET", "").strip() if secret is None else secret
        if self.emails and not secret:
            _log(_for_owner("[magic-link] SESSION_SECRET is not set: everyone will have to sign in again after a restart",
                        "[magic-link] SESSION_SECRET не задан: после перезапуска приложения всем придётся войти заново"))
        self.secret = (secret or secrets.token_hex(32)).encode("utf-8")
        self.request_url = os.environ.get("ACCESS_REQUEST_URL", "") if request_url is None else request_url
        self.cookie_name = cookie_name
        self.telegram = (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip())
        self.lang = os.environ.get("ACCESS_LANG", "auto").strip().lower()
        self.max_age = max_age
        self.used_links = {}  # одноразовые ссылки для входа: nonce -> срок
        self.attempts = {}  # ограничение частоты писем: ключ -> [время запросов]
        self.lock = threading.Lock()
        if not self.entries and not self.emails:
            _log(_for_owner("[magic-link] neither ACCESS_TOKEN nor ACCESS_EMAILS is set: nobody can get in",
                        "[magic-link] ACCESS_TOKEN и ACCESS_EMAILS не заданы: доступ закрыт для всех"))
        if self.emails and not _mail_mode():
            _log(_for_owner("[magic-link] email is not set up (RESEND_API_KEY or SMTP_URL): sign-in emails will not be "
                        "sent. Set it up: small mail",
                        "[magic-link] почта не настроена (RESEND_API_KEY или SMTP_URL): письма для входа не уйдут. "
                        "Подключить: small mail"))

    # --- общий обработчик: None — пропустить в приложение, иначе (код, заголовки, тело) ---

    def wants_body(self, method, path):
        return method == "POST" and ((path == REQUEST_PATH and not self.request_url)
                                     or (bool(self.emails) and path in (LOGIN_PATH, VERIFY_PATH)))

    def dispatch(self, method, base, path, path_bytes, query, cookie, ip, user_agent, host, https, body=b"",
                 accept_language=""):
        lang = pick_lang(accept_language, self.lang)
        if path == REQUEST_PATH and method == "POST" and not self.request_url:
            self.log_request(body, ip, user_agent, host)
            return self.respond(200, "sent", base, lang=lang)
        if self.emails and path == LOGIN_PATH and method == "POST":
            return self.login_request(body, ip, user_agent, host, https, base, lang)
        if self.emails and path == VERIFY_PATH:
            return self.verify(method, query, body, ip, https, base, lang)
        if path == LOGOUT_PATH:
            return self.respond(200, "logout" if self.emails else "forbidden", base,
                                [("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % self.cookie_name)],
                                lang=lang)

        token = next((value for key, value in query if key == "token"), None)
        if token is not None:
            name = self.token_owner(token)
            if name is None:
                return self.respond(403, "invalid", base, lang=lang)
            self.log_login(name, ip)
            return 302, [("Location", self.location(path_bytes, query)), ("Set-Cookie", self.set_cookie(token, https)),
                         ("Cache-Control", "no-store")], b""

        if self.cookie_owner(cookie) is not None:
            return None
        return self.respond(403, "forbidden", base, lang=lang)

    def respond(self, status, kind, base, headers=(), lang=DEFAULT_LANG, **page):
        return status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                        ("Referrer-Policy", "no-referrer"), ("Content-Language", lang)] + list(headers), \
            self.page(kind, base, lang=lang, **page)

    # --- ссылки-приглашения (ACCESS_TOKEN) ---

    def token_owner(self, token):
        """Имя владельца токена ("" — без имени) или None, если токен неверный."""
        return _owner(token, self.entries)

    def cookie_owner(self, cookie_header):
        """Кто вошёл: имя из ACCESS_TOKEN ("" — без имени), адрес почты или None."""
        try:
            morsel = SimpleCookie(cookie_header or "").get(self.cookie_name)
        except CookieError:
            return None
        return None if morsel is None else self.session_owner(morsel.value)

    def session_owner(self, value):
        """То же по значению куки: "e.<подпись>" — сессия входа по почте, иначе производное от токена."""
        value = value if isinstance(value, str) else ""
        if value.startswith("e."):
            data = self.unsign("session", value[2:])
            email = data.get("e") if data else None
            return email if isinstance(email, str) and email and self.email_allowed(email) else None
        return _owner(value, self.cookies)

    def set_cookie(self, token, https):
        return self._cookie(_cookie_value(token), https)

    def _cookie(self, value, https):
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (
            self.cookie_name, value, self.max_age, "; Secure" if https else "")

    @staticmethod
    def location(path_bytes, query):
        # Убираем токен из адреса, чтобы он не оставался в истории браузера и не утекал через Referer.
        # Ведущие слэши схлопываем: Location "//host" был бы открытым редиректом.
        location = "/" + quote(path_bytes, safe="/").lstrip("/")
        rest = urlencode([(k, v) for k, v in query if k != "token"])
        return location + ("?" + rest if rest else "")

    # --- вход по почте (ACCESS_EMAILS) ---

    def email_allowed(self, email):
        email = email.lower()
        return email in self.emails or "@" + email.rpartition("@")[2] in self.emails

    def sign(self, purpose, data):
        payload = _b64(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        return payload + "." + self._mac(purpose, payload)

    def unsign(self, purpose, value):
        """Данные из подписанной строки, если подпись верна и срок не вышел; иначе None."""
        payload, _, mac = (value or "").partition(".")
        if not payload or not hmac.compare_digest(mac.encode("ascii", "replace"), self._mac(purpose, payload).encode()):
            return None
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("x"), int) or data["x"] < time.time():
            return None
        return data

    def _mac(self, purpose, payload):
        # purpose разводит ссылку для входа и сессию: одну нельзя выдать за другую.
        return _b64(hmac.new(self.secret, (purpose + "|" + payload).encode("utf-8"), hashlib.sha256).digest())

    def login_request(self, body, ip, user_agent, host, https, base, lang=DEFAULT_LANG):
        email = dict(parse_qsl(body.decode("utf-8", "replace"))).get("email", "")
        kind = self.start_login(email, ip, user_agent, host, https, base, lang, VERIFY_PATH)
        return self.respond({"bad_email": 400, "slow": 429}.get(kind, 200), kind, base, lang=lang)

    def start_login(self, email, ip, user_agent, host, https, base, lang, path):
        """Письмо со ссылкой для входа на страницу path. Итог: "check" — одинаковый для известных и неизвестных
        адресов (неизвестный — это запрос доступа), "bad_email" или "slow" (превышен лимит писем)."""
        email = (email if isinstance(email, str) else "").strip().lower()[:254]
        if not EMAIL_RE.match(email):
            return "bad_email"
        if not (self._allow("ip:" + ip, LIMIT_PER_IP) and self._allow("mail:" + email, LIMIT_PER_EMAIL)):
            return "slow"
        if self.email_allowed(email):
            app_url = self.public_url(host, https, base)
            if app_url is None:
                _log(_for_owner("[magic-link] sign-in email not sent: the app's address is unknown. Set PUBLIC_URL",
                            "[magic-link] письмо для входа не отправлено: неизвестен адрес приложения. Задайте PUBLIC_URL"))
            else:
                link = self.login_link(app_url + path, email)
                # В фоне: ответ не ждёт почту и одинаков для известных и неизвестных адресов.
                threading.Thread(target=_send_login_mail, args=(email, link, urlsplit(app_url).netloc, lang),
                                 daemon=True).start()
        else:
            self.notify_request(email, ip, user_agent, host)
        return "check"

    def login_link(self, url, email):
        """Одноразовая ссылка для входа: действует LINK_TTL секунд."""
        return url + "?" + urlencode({"t": self.sign("login", {
            "e": email, "x": int(time.time()) + LINK_TTL, "n": secrets.token_urlsafe(9)})})

    def login_data(self, token):
        """Данные действующей ссылки для входа (подпись верна, срок не вышел, адрес в списке, ещё не использована)."""
        data = self.unsign("login", token if isinstance(token, str) else "")
        email = data.get("e") if data else None
        if not (isinstance(email, str) and email and self.email_allowed(email) and data.get("n") not in self.used_links):
            return None
        return data

    def use_login(self, token, ip):
        """Тратит ссылку для входа: значение куки сессии "e.<подпись>" или None, если ссылка не годится."""
        data = self.login_data(token)
        if data is None:
            return None
        with self.lock:
            now = time.time()
            self.used_links = {n: x for n, x in self.used_links.items() if x > now}
            if data["n"] in self.used_links:
                return None
            self.used_links[data["n"]] = data["x"]
        _log(_for_owner("[magic-link] signed in by email (%s), ip=%s", "[magic-link] вход по почте (%s), ip=%s")
             % (data["e"], ip))
        return "e." + self.sign("session", {"e": data["e"], "x": int(time.time()) + self.max_age})

    def verify(self, method, query, body, ip, https, base, lang=DEFAULT_LANG):
        source = parse_qsl(body.decode("utf-8", "replace")) if method == "POST" else query
        token = dict(source).get("t", "")
        data = self.login_data(token)
        if data is None:
            return self.respond(403, "expired", base, lang=lang)
        if method != "POST":
            # Почтовые сканеры сами открывают ссылки из писем. Вход — только по кнопке, иначе сканер «потратит» ссылку.
            return self.respond(200, "confirm", base, lang=lang, email=data["e"], token=token)
        session = self.use_login(token, ip)
        if session is None:
            return self.respond(403, "expired", base, lang=lang)
        return 302, [("Location", (base or "") + "/"), ("Set-Cookie", self._cookie(session, https)),
                     ("Cache-Control", "no-store")], b""

    def public_url(self, host, https, base):
        """Адрес приложения для ссылки в письме. Заголовку Host верим только локально: иначе злоумышленник
        подставил бы свой домен, и ссылка для входа из настоящего письма ушла бы к нему."""
        explicit = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
        if explicit:
            return explicit
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if domain:
            return "https://" + domain + base
        hostname = (host or "").rsplit(":", 1)[0].strip("[]").lower()
        if hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".localhost"):
            return ("https://" if https else "http://") + host + base
        return None

    def _allow(self, key, limit):
        now = time.time()
        with self.lock:
            if len(self.attempts) > 10000:
                self.attempts = {k: v for k, v in self.attempts.items() if v[-1] > now - LIMIT_WINDOW}
            recent = [t for t in self.attempts.get(key, []) if t > now - LIMIT_WINDOW]
            if len(recent) >= limit:
                self.attempts[key] = recent
                return False
            self.attempts[key] = recent + [now]
            return True

    # --- страницы и журнал ---

    def page(self, kind, base, email="", token="", lang=DEFAULT_LANG):
        if kind == "forbidden" and self.emails:
            kind = "login"
        icon, title, text = MESSAGES[lang][kind]
        ui = UI[lang]
        if kind == "confirm":
            text = text % email
        if kind == "sent":
            action = ""
        elif kind == "check":
            action = '<a class="link" href="%s/">%s</a>' % (html.escape(base), ui["other"])
        elif kind == "confirm":
            action = CONFIRM_FORM.substitute(action=html.escape(base + VERIFY_PATH), token=html.escape(token),
                                             button=ui["sign_in"])
        elif self.emails:
            action = LOGIN_FORM.substitute(action=html.escape(base + LOGIN_PATH), button=ui["get_link"])
        elif self.request_url:
            action = '<a class="btn" href="%s">%s</a>' % (html.escape(self.request_url), ui["request"])
        else:
            action = REQUEST_FORM.substitute(action=html.escape(base + REQUEST_PATH), placeholder=ui["contact"],
                                             button=ui["request"])
        return PAGE.substitute(lang=lang, icon=icon, title=html.escape(title), text=html.escape(text),
                               action=action).encode("utf-8")

    def log_request(self, body, ip, user_agent, host=""):
        contact = dict(parse_qsl(body.decode("utf-8", "replace"))).get("contact", "")[:200]
        self.notify_request(contact, ip, user_agent, host)

    def notify_request(self, contact, ip, user_agent, host=""):
        # repr() экранирует переводы строк: пользователь не сможет подделать строки лога.
        _log("[access-request] contact=%r ip=%s ua=%r" % (contact, ip, user_agent[:200]))
        token, chat = self.telegram
        if token and chat:
            hint = "small access add " + (contact if EMAIL_RE.match(contact) else _for_owner("NAME", "ИМЯ"))
            text = _for_owner("🔑 Access request to %s\nContact: %s\nIP: %s\n\nGrant access: %s",
                          "🔑 Запрос доступа к %s\nКонтакт: %s\nIP: %s\n\nВыдать доступ: %s") % (
                host or _for_owner("the app", "приложению"), contact or "—", ip or "—", hint)
            # В фоне: пользователь не ждёт Telegram. Без parse_mode — текст из формы ничего не разметит.
            threading.Thread(target=_send_telegram, args=(token, chat, text), daemon=True).start()

    @staticmethod
    def log_login(name, ip):
        _log(_for_owner("[magic-link] signed in by link%s, ip=%s", "[magic-link] вход по ссылке%s, ip=%s") % (
            " (%s)" % name if name else "", ip))


class MagicLink:
    """WSGI-middleware."""

    def __init__(self, app, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE,
                 emails=None, secret=None):
        self.app = app
        self.gate = _Gate(tokens, request_url, cookie_name, max_age, emails, secret)

    def __call__(self, environ, start_response):
        gate = self.gate
        method = environ.get("REQUEST_METHOD", "GET")
        base = environ.get("SCRIPT_NAME", "")
        path = environ.get("PATH_INFO", "") or "/"
        body = b""
        if gate.wants_body(method, path):
            try:
                size = min(int(environ.get("CONTENT_LENGTH") or 0), 4096)
            except ValueError:
                size = 0
            body = environ["wsgi.input"].read(size)
        forwarded = environ.get("HTTP_X_FORWARDED_FOR")
        result = gate.dispatch(
            method, base, path, (base + path).encode("latin-1", "replace"),
            parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True), environ.get("HTTP_COOKIE", ""),
            _client_ip(forwarded, environ.get("REMOTE_ADDR")), environ.get("HTTP_USER_AGENT", ""),
            environ.get("HTTP_HOST", ""), _is_https(environ.get("HTTP_X_FORWARDED_PROTO"), environ.get("wsgi.url_scheme")),
            body, environ.get("HTTP_ACCEPT_LANGUAGE", ""))
        if result is None:
            return self.app(environ, start_response)
        status, headers, payload = result
        start_response(STATUS[status], headers + [("Content-Length", str(len(payload)))])
        return [payload]


class MagicLinkASGI:
    """ASGI-middleware (FastAPI, Starlette, Litestar...). Websocket без доступа закрывается до accept (403)."""

    def __init__(self, app, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE,
                 emails=None, secret=None):
        self.app = app
        self.gate = _Gate(tokens, request_url, cookie_name, max_age, emails, secret)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):  # lifespan и прочее — без проверки
            return await self.app(scope, receive, send)
        gate = self.gate
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        if scope["type"] == "websocket":
            if gate.cookie_owner(headers.get("cookie", "")) is not None:
                return await self.app(scope, receive, send)
            return await send({"type": "websocket.close", "code": 1008})

        method = scope.get("method", "GET")
        base = scope.get("root_path", "")
        path = scope.get("path", "") or "/"
        body = b""
        if gate.wants_body(method, path):
            while len(body) < 4096:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
        client = scope.get("client") or ("", 0)
        result = gate.dispatch(
            method, base, path, (base + path).encode("utf-8"),
            parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True),
            headers.get("cookie", ""), _client_ip(headers.get("x-forwarded-for"), client[0]),
            headers.get("user-agent", ""), headers.get("host", ""),
            _is_https(headers.get("x-forwarded-proto"), scope.get("scheme")), body[:4096],
            headers.get("accept-language", ""))
        if result is None:
            return await self.app(scope, receive, send)
        status, response_headers, payload = result
        await send({"type": "http.response.start", "status": status, "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in response_headers
        ] + [(b"content-length", str(len(payload)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": payload})


class MagicLinkGate:
    """Та же защита для фреймворков, которые нельзя обернуть middleware (например, Streamlit).

    Те же переменные окружения, подписи, одноразовые ссылки, лимиты писем, журнал и тексты, что у MagicLink, но без
    HTTP: формы показывает само приложение, и куку сессии (gate.cookie_name) оно же ставит и читает. Создавайте один
    объект на процесс — в нём хранятся использованные ссылки и счётчики писем.

        gate = MagicLinkGate()
        gate.session_owner(cookie_value)       # кто вошёл по значению куки: адрес, имя из ACCESS_TOKEN или None
        gate.token_login(token, headers)       # ?token=: имя ("" — общая ссылка) или None
        gate.start_login(email, headers)       # письмо со ссылкой ?t=: "check" для любого адреса, "bad_email", "slow"
        gate.login_email(t)                    # кому выдана ссылка ?t= (ссылку не тратит) или None
        gate.finish_login(t, headers)          # только по нажатию кнопки: тратит ссылку, значение куки или None
        gate.request_access(contact, headers)  # запрос доступа: в лог и в Telegram
    headers — заголовки запроса (любой Mapping, регистр не важен): из них берутся IP, Host, User-Agent и язык.
    """

    def __init__(self, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE,
                 emails=None, secret=None):
        self._gate = _Gate(tokens, request_url, cookie_name, max_age, emails, secret)
        self.cookie_name, self.max_age = cookie_name, max_age

    @property
    def emails(self):
        """Адреса и домены из ACCESS_EMAILS. Пусто — входа по почте нет."""
        return frozenset(self._gate.emails)

    @property
    def request_url(self):
        """ACCESS_REQUEST_URL: куда ведёт кнопка «Запросить доступ»; "" — встроенная форма запроса."""
        return self._gate.request_url

    def lang(self, headers=None):
        """Язык посетителя: ACCESS_LANG, иначе по Accept-Language — "ru" или "en"."""
        return pick_lang(_header(headers, "accept-language"), self._gate.lang)

    @staticmethod
    def text(kind, lang, email=""):
        """(значок, заголовок, текст) из MESSAGES — обычным текстом, без HTML-сущностей."""
        icon, title, text = MESSAGES[lang if lang in MESSAGES else DEFAULT_LANG][kind]
        return html.unescape(icon), title, (text % email if kind == "confirm" else text)

    @staticmethod
    def label(name, lang):
        """Подпись кнопки или поля из UI: "request", "contact", "get_link", "sign_in", "other"."""
        return UI[lang if lang in UI else DEFAULT_LANG][name]

    def token_login(self, token, headers=None):
        """Вход по ссылке-приглашению ?token=: имя из ACCESS_TOKEN ("" — без имени) или None. Вход пишется в лог."""
        name = self._gate.token_owner(token if isinstance(token, str) else "")
        if name is not None:
            self._gate.log_login(name, _request_info(headers)[0])
        return name

    def session_owner(self, value):
        """Кто вошёл по значению куки gate.cookie_name: адрес почты, имя из ACCESS_TOKEN или None. Убрали адрес из
        ACCESS_EMAILS или сменили SESSION_SECRET — сессия больше не подходит."""
        return self._gate.session_owner(value)

    def start_login(self, email, headers=None, path="/"):
        """Отправляет письмо со ссылкой <адрес приложения><path>?t=... Адрес берётся из PUBLIC_URL или
        RAILWAY_PUBLIC_DOMAIN, заголовку Host верим только для localhost. Итог: "check" — одинаковый для известных
        и неизвестных адресов (неизвестный становится запросом доступа), "bad_email" или "slow"."""
        ip, user_agent, host, https = _request_info(headers)
        return self._gate.start_login(email, ip, user_agent, host, https, "", self.lang(headers), path)

    def login_link(self, email, app_url, path="/"):
        """Ссылка для входа без письма (для проверок и своих способов доставки): <app_url><path>?t=..."""
        return self._gate.login_link(app_url.rstrip("/") + path, email.strip().lower())

    def login_email(self, token):
        """Адрес из действующей ссылки для входа или None. Ссылку не тратит: её открывают и почтовые сканеры,
        поэтому вход — только по кнопке (finish_login)."""
        data = self._gate.login_data(token)
        return data["e"] if data else None

    def finish_login(self, token, headers=None):
        """Тратит ссылку для входа (один раз, под замком): значение куки сессии или None. Вход пишется в лог."""
        return self._gate.use_login(token, _request_info(headers)[0])

    def request_access(self, contact, headers=None):
        """Запрос доступа: строка [access-request] в лог (repr — строки лога не подделать) и сообщение в Telegram."""
        ip, user_agent, host, _ = _request_info(headers)
        self._gate.notify_request((contact if isinstance(contact, str) else "").strip()[:200], ip, user_agent, host)


def _header(headers, name):
    for key, value in (headers or {}).items():
        if str(key).lower() == name and isinstance(value, str):
            return value
    return ""


def _request_info(headers):
    """(ip, user_agent, host, https) из заголовков запроса — для MagicLinkGate."""
    return (_client_ip(_header(headers, "x-forwarded-for"), ""), _header(headers, "user-agent"),
            _header(headers, "host"), _is_https(_header(headers, "x-forwarded-proto"), None))


def _owner(value, entries):
    value = value.encode("utf-8")
    for name, candidate in entries:
        if hmac.compare_digest(value, candidate.encode("utf-8")):
            return name
    return None


def _cookie_value(token):
    # В куке лежит не сам токен, а производное от него: убрали токен из ACCESS_TOKEN — его куки больше не подходят.
    return hmac.new(token.encode("utf-8"), b"magic-link-cookie", hashlib.sha256).hexdigest()


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text):
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(e)


def _client_ip(forwarded, remote):
    return (forwarded or remote or "").split(",")[0].strip()


def _is_https(forwarded_proto, scheme):
    return (forwarded_proto or scheme or "http").split(",")[0].strip() == "https"


def _mail_mode():
    """Чем отправлять письма для входа: "log", "resend", "smtp" или "" — не настроено."""
    if os.environ.get("LOGIN_LINKS_TO_LOG", "").strip().lower() in ("1", "true", "yes"):
        return "log"
    if os.environ.get("RESEND_API_KEY", "").strip():
        return "resend"
    if os.environ.get("SMTP_URL", "").strip():
        return "smtp"
    return ""


def _send_login_mail(email, link, app_name, lang=DEFAULT_LANG):
    mode = _mail_mode()
    if mode == "log":
        _log(_for_owner("[magic-link] sign-in link (%s): %s", "[magic-link] ссылка для входа (%s): %s") % (email, link))
        return
    ui = UI[lang if lang in UI else DEFAULT_LANG]
    body = '<p>%s</p><p><a href="%s">%s</a></p><p style="color:#666">%s</p>' % (
        ui["mail_intro"] % html.escape(app_name), html.escape(link), ui["mail_link"], ui["mail_note"])
    try:
        send_mail(email, ui["subject"] % app_name, ui["mail_text"] % (app_name, link), body)
        _log(_for_owner("[magic-link] sign-in email sent: %s", "[magic-link] письмо для входа отправлено: %s") % email)
    except Exception as e:  # письмо — не повод ронять приложение; ключи и пароли в лог не пишем
        _log(_for_owner("[magic-link] could not send the sign-in email to %s: %s",
                    "[magic-link] не удалось отправить письмо для входа на %s: %s") % (email, _describe(e)))


def send_mail(to, subject, text, html_body):
    """Письмо через Resend (RESEND_API_KEY) или SMTP (SMTP_URL). Ошибка — исключение."""
    sender = os.environ.get("MAIL_FROM", "").strip()
    key = os.environ.get("RESEND_API_KEY", "").strip()
    smtp_url = os.environ.get("SMTP_URL", "").strip()
    if key:
        api = os.environ.get("RESEND_API_URL", "https://api.resend.com").rstrip("/")
        request = urllib.request.Request(api + "/emails", headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "small-magic-link/1.0",
        }, data=json.dumps({"from": sender or "small <onboarding@resend.dev>", "to": [to], "subject": subject,
                            "text": text, "html": html_body}).encode("utf-8"))
        urllib.request.urlopen(request, timeout=15).close()
    elif smtp_url:
        url = urlsplit(smtp_url)
        secure = url.scheme == "smtps"
        message = EmailMessage()
        message["From"] = sender or unquote(url.username or "")
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(html_body, subtype="html")
        with (smtplib.SMTP_SSL if secure else smtplib.SMTP)(url.hostname, url.port or (465 if secure else 587),
                                                             timeout=15) as server:
            if not secure:
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls()
                    server.ehlo()
            if url.username:
                server.login(unquote(url.username), unquote(url.password or ""))
            server.send_message(message)
    else:
        raise RuntimeError(_for_owner("email is not set up: set RESEND_API_KEY or SMTP_URL",
                                  "почта не настроена: задайте RESEND_API_KEY или SMTP_URL"))


def _describe(error):
    if isinstance(error, urllib.error.HTTPError):
        try:
            detail = error.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return "HTTP %d %s" % (error.code, detail)
    return "%s: %s" % (type(error).__name__, str(error)[:200])


def _send_telegram(token, chat, text):
    api = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
    request = urllib.request.Request("%s/bot%s/sendMessage" % (api, token), headers={"Content-Type": "application/json"},
                                     data=json.dumps({"chat_id": chat, "text": text}).encode("utf-8"))
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except Exception as e:  # уведомление — не повод ронять запрос; токен бота в лог не пишем
        _log(_for_owner("[magic-link] could not send the access request to Telegram: %s",
                    "[magic-link] не удалось отправить запрос доступа в Telegram: %s") % type(e).__name__)


def _for_owner(en, ru):
    """Текст для владельца (лог, Telegram) — на его языке, OWNER_LANG. Посетитель видит язык своего браузера."""
    return ru if os.environ.get("OWNER_LANG", "").strip().lower()[:2] == "ru" else en


def _log(message):
    print(message, flush=True)


REQUEST_FORM = Template(
    '<form method="post" action="$action">'
    '<input name="contact" required maxlength="200" placeholder="$placeholder">'
    '<button class="btn" type="submit">$button</button></form>'
)

LOGIN_FORM = Template(
    '<form method="post" action="$action">'
    '<input name="email" type="email" required maxlength="254" autocomplete="email" placeholder="name@company.com">'
    '<button class="btn" type="submit">$button</button></form>'
)

CONFIRM_FORM = Template(
    '<form method="post" action="$action"><input type="hidden" name="t" value="$token">'
    '<button class="btn" type="submit">$button</button></form>'
)

PAGE = Template("""<!doctype html>
<html lang="$lang">
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
  p { margin: 0 0 24px; color: var(--muted); overflow-wrap: anywhere; }
  input { width: 100%; margin-bottom: 12px; padding: 10px 12px; font: inherit; color: inherit;
          background: transparent; border: 1px solid var(--border); border-radius: 8px; }
  .btn { display: block; width: 100%; padding: 10px 16px; font: inherit; font-weight: 600; color: #fff;
         background: var(--accent); border: 0; border-radius: 8px; text-decoration: none; cursor: pointer; }
  .btn:hover { filter: brightness(1.1); }
  .link { color: var(--accent); }
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
