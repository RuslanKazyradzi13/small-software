"""Protect a web app: sign in with a work account, a work email or an invitation link. One file, no dependencies,
Python 3.8+.

    from magic_link import MagicLink, MagicLinkASGI
    application = MagicLink(application)        # WSGI: Django; Flask: app.wsgi_app = MagicLink(app.wsgi_app)
    app.add_middleware(MagicLinkASGI)           # ASGI: FastAPI, Starlette; or app = MagicLinkASGI(app)
    gate = MagicLinkGate()                      # no middleware (Streamlit etc.): see the MagicLinkGate class

Three ways to let someone in. The owner lists who may come in ACCESS_EMAILS, like sharing a Google Doc; an unknown
address becomes an access request.
  * Single sign-on with Google or Microsoft (OpenID Connect): "Sign in with Google" / "Sign in with Microsoft"
    buttons. The address comes from an ID token that the app gets straight from the provider; see _identity for
    which accounts count as work accounts.
  * Email sign-in: a person enters their email and gets a sign-in link (valid for 15 minutes, once). Only someone
    who reads that inbox can sign in.
  * Invitation link https://<app>/?token=<token> (ACCESS_TOKEN): the middleware sets a cookie and redirects
    to the same address without the token. The link works like a password: anyone it is forwarded to gets in.
The browser remembers a sign-in for 30 days. Without access: a 403 page with the sign-in options (if ACCESS_EMAILS is
set) or a "Request access" button. Pages and emails are in Russian or English, depending on the browser language.
The app learns who is signed in from environ["magic_link.user"] (WSGI) or scope["magic_link.user"] (ASGI), and how
from "magic_link.method": "email", "google", "microsoft" or "link".

Behind the small gateway (small gateway on) the gateway does all of the above, and this file in the app only checks
that a request came through it: SMALL_GATEWAY_KEY set means "trust the gateway's signed X-Small-User, X-Small-Method
and X-Small-Auth headers, and nothing else".
Sign-ins, refusals and access requests are logged as single-line JSON (Railway shows them as structured logs,
`small access log` reads them).

Environment variables:
    ACCESS_EMAILS       comma-separated addresses; "@company.com" means the whole domain. Remove an address and
                        sign-in stops working immediately, including with a cookie already issued.
    SESSION_SECRET      key that signs sign-in links and sessions (small generates it). If unset, a random one is
                        made on every start: after a restart everyone has to sign in again.
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
                        "Sign in with Google" (set up with: small sso google)
    MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET, MICROSOFT_TENANT_ID
                        "Sign in with Microsoft" for the members of one Microsoft Entra tenant (small sso microsoft)
    RESEND_API_KEY      send email through Resend (HTTPS, so it works on Railway Hobby too);
    MAIL_FROM           sender, e.g. "Reports <noreply@company.com>" (the domain must be verified in Resend)
    SMTP_URL            smtp://user:password@host:587 or smtps://...:465 (Railway allows SMTP only from the Pro plan)
    LOGIN_LINKS_TO_LOG  1: don't send email, write the sign-in link to the log instead. For testing only.
    PUBLIC_URL          app address used in email links and sign-on redirects (e.g. your own domain). If unset,
                        RAILWAY_PUBLIC_DOMAIN is used; locally, localhost.
    ACCESS_DB           SQLite file for sign-in state: used sign-in links, rate limits, "sign out on all devices".
                        Default: .magic-link.db on the Railway volume (RAILWAY_VOLUME_MOUNT_PATH, see small volume);
                        without a volume the state is kept in memory and a restart forgets it.
    SESSIONS_REVOKED    "ivan@company.com=1760000000, @company.com=..., *=...": sessions of that address, domain or
                        everyone issued before that Unix time no longer work (small access signout keeps it)
    ACCESS_LANG         language of pages and emails: ru, en or auto (default: from the Accept-Language header)
    OWNER_LANG          language of messages for the owner (log lines and Telegram notifications): en (default) or
                        ru. small deploy sets it to match small's language.
    ACCESS_TOKEN        comma-separated invitation-link tokens, optionally named: "alice:token1,bob:token2".
                        Remove someone's token and their link, plus any cookies issued through it, stop working.
    ACCESS_REQUEST_URL  where the "Request access" button leads (mailto:..., https://t.me/...) when there is no
                        email sign-in. If unset, the request is printed to the log as an [access-request] line
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
                        access requests reach the owner in Telegram (set up with: small notify telegram)
    SMALL_GATEWAY_KEY   the app is behind the small gateway: let in only requests it signed with this key (small
                        gateway on sets it); the variables above then belong to the gateway
"""

import asyncio
import base64
import functools
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
SSO_PATH = "/_access/sso/"                # /_access/sso/google starts single sign-on with Google
SSO_CALLBACK = "/_access/sso/callback"    # the redirect URI to register with Google and Microsoft
SSO_TTL = 10 * 60                         # how long a started single sign-on may take
CLOCK_SKEW = 5 * 60                       # leeway for the provider's clock when checking ID token times
GATEWAY_TTL = 24 * 3600                   # how long the gateway's signature on a request stays valid
GATEWAY_HEADERS = ("x-small-user", "x-small-method", "x-small-auth")  # what the small gateway adds for the app
# How many sign-in emails can be requested within 15 minutes: per address and per IP.
LIMIT_PER_EMAIL, LIMIT_PER_IP, LIMIT_WINDOW = 3, 10, 15 * 60

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
DOMAIN_RE = re.compile(r"^@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
STATUS = {200: "200 OK", 302: "302 Found", 400: "400 Bad Request", 403: "403 Forbidden", 429: "429 Too Many Requests",
          500: "500 Internal Server Error"}
LANGS = ("ru", "en")
DEFAULT_LANG = "ru"  # when the browser doesn't say which language it prefers

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
MICROSOFT_LOGIN_URL = "https://login.microsoftonline.com"
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Our pages have no scripts, can't be framed and submit forms only to the app itself.
PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")

MESSAGES = {
    "ru": {
        "forbidden": ("&#128274;", "Доступ по приглашению",
                      "Эта страница открывается только по ссылке-приглашению от владельца."),
        "invalid": ("&#128274;", "Ссылка недействительна",
                    "Ссылка устарела или скопирована не полностью. Запросите новую."),
        "sent": ("&#9989;", "Запрос отправлен", "Владелец увидит запрос и пришлёт вам ссылку-приглашение."),
        "login": ("&#128274;", "Вход по рабочей почте",
                  "Приложение открывается только для приглашённых. Введите рабочую почту — пришлём ссылку для входа."),
        "sso": ("&#128274;", "Вход рабочим аккаунтом", "Приложение открывается только для приглашённых."),
        "check": ("&#9993;&#65039;", "Проверьте почту",
                  "Если у адреса есть доступ, ссылка для входа уже в почте: она действует 15 минут. "
                  "Если доступа нет, владелец получит ваш запрос."),
        "bad_email": ("&#9888;&#65039;", "Проверьте адрес", "Похоже, в адресе почты ошибка. Введите его ещё раз."),
        "slow": ("&#8987;", "Слишком много попыток", "Подождите 15 минут и попробуйте снова."),
        "expired": ("&#8987;", "Ссылка для входа устарела",
                    "Ссылка действует 15 минут и только один раз. Введите почту — пришлём новую."),
        "confirm": ("&#128274;", "Вход в приложение", "Нажмите кнопку, чтобы войти как %s."),
        "logout": ("&#128075;", "Вы вышли", "Вы вышли из приложения в этом браузере."),
        "logout_all": ("&#128075;", "Вы вышли на всех устройствах",
                       "Все входы с адресом %s завершены. Чтобы вернуться, войдите снова."),
        "denied": ("&#128683;", "Нет доступа", "У %s нет доступа к этому приложению. Владелец получил ваш запрос."),
        "unverified": ("&#9888;&#65039;", "Нужен рабочий аккаунт",
                       "Этот аккаунт не подтверждает, что адрес %s принадлежит вашей организации. "
                       "Войдите рабочим аккаунтом."),
        "sso_failed": ("&#9888;&#65039;", "Не удалось войти", "Вход не завершился. Попробуйте ещё раз."),
        "gateway": ("&#128274;", "Откройте приложение по его адресу",
                    "Это приложение открывается только через свой адрес, где проверяется вход."),
    },
    "en": {
        "forbidden": ("&#128274;", "Invitation only", "This page opens only with an invitation link from its owner."),
        "invalid": ("&#128274;", "Invalid link", "The link has expired or was copied incompletely. Ask for a new one."),
        "sent": ("&#9989;", "Request sent", "The owner will see your request and send you an invitation link."),
        "login": ("&#128274;", "Sign in with your work email",
                  "This app is for invited people only. Enter your work email and we'll send you a sign-in link."),
        "sso": ("&#128274;", "Sign in with your work account", "This app is for invited people only."),
        "check": ("&#9993;&#65039;", "Check your email",
                  "If this address has access, a sign-in link is already in your inbox. It works for 15 minutes. "
                  "If it doesn't, the owner will get your request."),
        "bad_email": ("&#9888;&#65039;", "Check the address", "This email address looks wrong. Please enter it again."),
        "slow": ("&#8987;", "Too many attempts", "Please wait 15 minutes and try again."),
        "expired": ("&#8987;", "Sign-in link expired",
                    "The link works for 15 minutes and only once. Enter your email to get a new one."),
        "confirm": ("&#128274;", "Sign in", "Click the button to sign in as %s."),
        "logout": ("&#128075;", "Signed out", "You are no longer signed in to the app in this browser."),
        "logout_all": ("&#128075;", "Signed out everywhere",
                       "Every sign-in as %s has ended, on all devices. Sign in again to come back."),
        "denied": ("&#128683;", "No access", "%s has no access to this app. The owner has received your request."),
        "unverified": ("&#9888;&#65039;", "Use your work account",
                       "This account doesn't confirm that %s belongs to your organization. "
                       "Sign in with your work account."),
        "sso_failed": ("&#9888;&#65039;", "Sign-in failed", "Sign-in didn't complete. Please try again."),
        "gateway": ("&#128274;", "Open the app at its address",
                    "This app opens only through its own address, where sign-in is checked."),
    },
}

UI = {
    "ru": {"request": "Запросить доступ", "contact": "Email или Telegram для ответа",
           "get_link": "Получить ссылку для входа", "sign_in": "Войти", "other": "Ввести другой адрес",
           "google": "Войти через Google", "microsoft": "Войти через Microsoft", "or": "или по ссылке на почту",
           "everywhere": "Выйти на всех устройствах", "other_account": "Войти другим аккаунтом",
           "subject": "Вход в %s", "mail_link": "Войти",
           "mail_text": "Ссылка для входа в %s:\n%s\n\nОна действует 15 минут и только один раз. "
                        "Если вы не запрашивали вход, просто удалите это письмо.\n",
           "mail_intro": "Ссылка для входа в <b>%s</b>:",
           "mail_note": "Она действует 15 минут и только один раз. Если вы не запрашивали вход, просто удалите это письмо."},
    "en": {"request": "Request access", "contact": "Email or Telegram to reach you",
           "get_link": "Email me a sign-in link", "sign_in": "Sign in", "other": "Use a different address",
           "google": "Sign in with Google", "microsoft": "Sign in with Microsoft", "or": "or get a link by email",
           "everywhere": "Sign out on all devices", "other_account": "Use a different account",
           "subject": "Sign in to %s", "mail_link": "Sign in",
           "mail_text": "Sign-in link for %s:\n%s\n\nIt works for 15 minutes and only once. "
                        "If you didn't ask to sign in, just delete this email.\n",
           "mail_intro": "Sign-in link for <b>%s</b>:",
           "mail_note": "It works for 15 minutes and only once. If you didn't ask to sign in, just delete this email."},
}

# What the owner reads in the log for each event: (English, Russian), see _Gate.audit.
AUDIT = {
    "sign_in": {"email": ("signed in by email", "вход по почте"), "link": ("signed in by link", "вход по ссылке"),
                "google": ("signed in with Google", "вход через Google"),
                "microsoft": ("signed in with Microsoft", "вход через Microsoft")},
    "denied": {"google": ("no access, Google sign-in refused", "нет доступа, вход через Google отклонён"),
               "microsoft": ("no access, Microsoft sign-in refused", "нет доступа, вход через Microsoft отклонён")},
    "sign_out_all": {"email": ("signed out on all devices", "выход на всех устройствах")},
}


def parse_tokens(value):
    """"alice:tok1, tok2" -> [("alice", "tok1"), ("", "tok2")]. The token itself must not contain a colon."""
    items = value.split(",") if isinstance(value, str) else value
    entries = []
    for item in items:
        name, _, token = item.strip().rpartition(":")
        if token.strip():
            entries.append((name.strip(), token.strip()))
    return entries


def parse_emails(value):
    """"Ivan@Company.com, @team.com, junk" -> {"ivan@company.com", "@team.com"}: addresses and domains, lowercased."""
    items = value.split(",") if isinstance(value, str) else value
    found = set()
    for item in items:
        item = item.strip().lower()
        if EMAIL_RE.match(item) or DOMAIN_RE.match(item):
            found.add(item)
    return found


def parse_revoked(value):
    """"ivan@company.com=1760000000, *=1760000100" -> {"ivan@company.com": 1760000000, "*": 1760000100}: address,
    @domain or * (everyone) -> Unix time before which their sessions are signed out. Junk is skipped."""
    found = {}
    for item in (value or "").split(","):
        who, _, moment = item.strip().lower().rpartition("=")
        who = who.strip()
        if (who == "*" or EMAIL_RE.match(who) or DOMAIN_RE.match(who)) and moment.strip().isdigit():
            found[who] = max(found.get(who, 0), int(moment))
    return found


def pick_lang(accept_language, setting="auto"):
    """Page language: ACCESS_LANG (ru/en), otherwise whichever of ru/en Accept-Language prefers most."""
    if setting in LANGS:
        return setting
    if not (accept_language or "").strip():
        return DEFAULT_LANG
    best, best_q = "en", -1.0  # browser wants some other language (de, fr...): English is a safer bet than Russian
    for part in (accept_language or "").split(","):
        tag, _, params = part.strip().partition(";")
        base = tag.strip().lower().split("-")[0]
        if base == "*":  # "any language" (Node's fetch sends this, for example)
            base = DEFAULT_LANG
        try:
            q = float(params.strip()[2:]) if params.strip().startswith("q=") else 1.0
        except ValueError:
            q = 0.0
        if base in LANGS and q > best_q:
            best, best_q = base, q
    return best


# --- single sign-on (OpenID Connect) ---

class SsoError(Exception):
    """Single sign-on didn't produce an address we trust. unverified: the account is real, but the provider doesn't
    vouch that the address belongs to an organization (a personal Google account with a company address, a guest)."""

    def __init__(self, reason, email="", unverified=False):
        super().__init__(reason)
        self.email, self.unverified = email, unverified


def sso_providers(environ=None):
    """Single sign-on providers set up in the environment, in button order: {"google": {...}, "microsoft": {...}}.

    A provider needs its client ID and secret; Microsoft also needs the tenant ID (a GUID): only that organization's
    accounts can sign in. *_AUTH_URL, *_TOKEN_URL and MICROSOFT_LOGIN_URL exist for tests."""
    env = os.environ if environ is None else environ
    get = lambda name: (env.get(name) or "").strip()  # noqa: E731
    found = {}
    if get("GOOGLE_CLIENT_ID") and get("GOOGLE_CLIENT_SECRET"):
        found["google"] = {"id": "google", "client_id": get("GOOGLE_CLIENT_ID"),
                           "client_secret": get("GOOGLE_CLIENT_SECRET"), "scope": "openid email",
                           "auth_url": get("GOOGLE_AUTH_URL") or GOOGLE_AUTH_URL,
                           "token_url": get("GOOGLE_TOKEN_URL") or GOOGLE_TOKEN_URL}
    tenant = get("MICROSOFT_TENANT_ID").lower()
    if get("MICROSOFT_CLIENT_ID") and get("MICROSOFT_CLIENT_SECRET") and GUID_RE.match(tenant):
        login = (get("MICROSOFT_LOGIN_URL") or MICROSOFT_LOGIN_URL).rstrip("/")
        found["microsoft"] = {"id": "microsoft", "client_id": get("MICROSOFT_CLIENT_ID"),
                              "client_secret": get("MICROSOFT_CLIENT_SECRET"), "scope": "openid profile email",
                              "tenant": tenant, "auth_url": "%s/%s/oauth2/v2.0/authorize" % (login, tenant),
                              "token_url": "%s/%s/oauth2/v2.0/token" % (login, tenant)}
    return found


def _identity(provider, claims, nonce, now=None):
    """The address from ID token claims, checked; otherwise SsoError.

    The token came straight from the provider's token endpoint over HTTPS with our client secret, so, as OpenID
    Connect Core 3.1.3.7 allows, TLS stands in for checking its signature; everything else is checked: audience,
    expiry, nonce, issuer. On top of that, the address must be one the provider vouches for:
      * Google: the address is verified, and it is a Gmail address or belongs to a Google Workspace organization (the
        hd claim). A personal Google account registered with a company address is refused: whoever made it may long
        have lost that mailbox.
      * Microsoft: a member of the configured tenant (issuer and tid match, not a guest from elsewhere). The address
        is the sign-in name (preferred_username, the UPN), whose domain the tenant has verified. The tenant's admins
        decide what that name is; tokens of any other tenant are refused, so another organization can't claim your
        addresses.
    """
    now = time.time() if now is None else now
    client_id = provider["client_id"]
    audience = claims.get("aud")
    if audience != client_id and not (isinstance(audience, list) and client_id in audience):
        raise SsoError("the ID token is for another app (aud)")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != client_id:
        raise SsoError("the ID token is for another app (azp)")
    expires, issued = claims.get("exp"), claims.get("iat")
    if not isinstance(expires, (int, float)) or expires < now - CLOCK_SKEW:
        raise SsoError("the ID token has expired")
    if isinstance(issued, (int, float)) and issued > now + CLOCK_SKEW:
        raise SsoError("the ID token is issued in the future")
    if not hmac.compare_digest(str(claims.get("nonce", "")).encode("utf-8"), nonce.encode("utf-8")):
        raise SsoError("the ID token belongs to another sign-in (nonce)")

    if provider["id"] == "google":
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise SsoError("the ID token is not from Google (iss)")
        email = claims.get("email")
        email = email.strip().lower() if isinstance(email, str) else ""
        if not EMAIL_RE.match(email):
            raise SsoError("Google sent no email address")
        if claims.get("email_verified") not in (True, "true"):
            raise SsoError("Google has not verified %s" % email, email, unverified=True)
        if not claims.get("hd") and email.rpartition("@")[2] not in GMAIL_DOMAINS:
            raise SsoError("%s is a personal Google account, not a Google Workspace one" % email, email, unverified=True)
        return email

    tenant = provider["tenant"]
    issuer = "https://login.microsoftonline.com/%s/v2.0" % tenant
    if claims.get("iss") != issuer or str(claims.get("tid", "")).lower() != tenant:
        raise SsoError("the ID token is from another Microsoft tenant")
    email = claims.get("preferred_username")
    email = email.strip().lower() if isinstance(email, str) else ""
    identity_provider = claims.get("idp")
    if identity_provider is not None and tenant not in str(identity_provider).lower():
        raise SsoError("%s is a guest in the tenant, not a member" % (email or "the account"), email, unverified=True)
    if "#ext#" in email or not EMAIL_RE.match(email):
        raise SsoError("Microsoft sent no work address", email, unverified=True)
    return email


def _exchange_code(provider, code, redirect_uri, verifier):
    """Claims of the ID token for an authorization code (with the client secret and the PKCE verifier)."""
    body = urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                      "client_id": provider["client_id"], "client_secret": provider["client_secret"],
                      "code_verifier": verifier}).encode("ascii")
    request = urllib.request.Request(provider["token_url"], data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": "small-magic-link/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            error = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            error = ""
        raise SsoError("the provider refused the code: HTTP %d %s" % (e.code, str(error)[:100])) from None
    except (OSError, ValueError) as e:
        raise SsoError("no answer from the provider: %s" % type(e).__name__) from None
    parts = payload.get("id_token", "").split(".") if isinstance(payload, dict) else []
    try:
        claims = json.loads(_unb64(parts[1])) if len(parts) == 3 else None
    except ValueError:
        claims = None
    if not isinstance(claims, dict):
        raise SsoError("the provider sent no ID token")
    return claims


# --- the small gateway: it signs who is signed in, the app behind it checks the signature ---

def gateway_auth(key, user, method, now=None):
    """X-Small-Auth for a request the gateway lets through: "<unix time>.<HMAC-SHA256 of user, method and time>".
    The app behind the gateway checks it with the same SMALL_GATEWAY_KEY, so a request that didn't come through the
    gateway (say, the app got a public address by mistake) can't claim to be someone."""
    moment = int(time.time() if now is None else now)
    return "%d.%s" % (moment, _b64(hmac.new(key.encode("utf-8"), ("small-gateway|%s|%s|%d" % (user, method, moment))
                                            .encode("utf-8"), hashlib.sha256).digest()))


def gateway_identity(key, user, method, auth, now=None):
    """(user, method) if X-Small-Auth is the gateway's signature of them, made within GATEWAY_TTL; otherwise None."""
    moment, _, _ = (auth or "").partition(".")
    if not (key and moment.isdigit() and isinstance(user, str) and isinstance(method, str)):
        return None
    now = time.time() if now is None else now
    if not now - GATEWAY_TTL <= int(moment) <= now + CLOCK_SKEW:
        return None
    expected = gateway_auth(key, user, method, int(moment))
    return (user, method) if hmac.compare_digest(expected.encode("utf-8"), auth.encode("utf-8", "replace")) else None


class _Pass:
    """dispatch's answer for a request that may go on to the app: who is signed in and how."""

    def __init__(self, user, method):
        self.user, self.method = user, method


# --- sign-in state: used links, rate limits, "sign out on all devices" ---

class _MemoryStore:
    """State in this process's memory: a restart forgets it, worker processes don't share it."""

    persistent = False

    def __init__(self):
        self.lock = threading.Lock()
        self.used, self.hits, self.signouts = {}, {}, {}

    def is_used(self, key):
        return key in self.used

    def use_once(self, key, expires):
        """True the first time for a key (until expires), False afterwards."""
        with self.lock:
            now = time.time()
            self.used = {k: x for k, x in self.used.items() if x > now}
            if key in self.used:
                return False
            self.used[key] = expires
            return True

    def allow(self, key, limit, window):
        """Count an attempt; False when there were already `limit` of them within `window` seconds."""
        now = time.time()
        with self.lock:
            if len(self.hits) > 10000:
                self.hits = {k: v for k, v in self.hits.items() if v[-1] > now - window}
            recent = [t for t in self.hits.get(key, []) if t > now - window]
            if len(recent) >= limit:
                self.hits[key] = recent
                return False
            self.hits[key] = recent + [now]
            return True

    def signed_out(self, email):
        return self.signouts.get(email, 0)

    def sign_out(self, email, moment):
        with self.lock:
            self.signouts[email] = max(self.signouts.get(email, 0), moment)


class _SQLiteStore(_MemoryStore):
    """State in a SQLite file (on a Railway volume): survives restarts and is shared by worker processes."""

    persistent = True

    def __init__(self, path):
        import sqlite3  # here: a Python built without sqlite3 still gets the memory store

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path, self.lock = path, threading.Lock()
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS used (key TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS hits (key TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS hits_key ON hits (key, at);
                CREATE TABLE IF NOT EXISTS signouts (email TEXT PRIMARY KEY, at REAL NOT NULL);
            """)

    def is_used(self, key):
        with self.lock:
            return self.db.execute("SELECT 1 FROM used WHERE key = ?", (key,)).fetchone() is not None

    def use_once(self, key, expires):
        with self.lock:
            self.db.execute("DELETE FROM used WHERE expires < ?", (int(time.time()),))
            # The primary key makes this atomic, across processes too.
            return self.db.execute("INSERT OR IGNORE INTO used (key, expires) VALUES (?, ?)",
                                   (key, int(expires))).rowcount == 1

    def allow(self, key, limit, window):
        now = time.time()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM hits WHERE at <= ?", (now - window,))
                count = self.db.execute("SELECT COUNT(*) FROM hits WHERE key = ?", (key,)).fetchone()[0]
                if count < limit:
                    self.db.execute("INSERT INTO hits (key, at) VALUES (?, ?)", (key, now))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return count < limit

    def signed_out(self, email):
        with self.lock:
            row = self.db.execute("SELECT at FROM signouts WHERE email = ?", (email,)).fetchone()
        return row[0] if row else 0

    def sign_out(self, email, moment):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO signouts (email, at) VALUES (?, MAX(?, COALESCE("
                            "(SELECT at FROM signouts WHERE email = ?), 0)))", (email, moment, email))


def open_store(path=None):
    """Where sign-in state lives: ACCESS_DB, else .magic-link.db on the Railway volume, else memory."""
    path = os.environ.get("ACCESS_DB", "").strip() if path is None else path
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if not path and mount:
        path = os.path.join(mount, ".magic-link.db")
    if not path or path == "memory":
        return _MemoryStore()
    try:
        return _SQLiteStore(path)
    except Exception as e:  # a broken volume should not take the app down: fall back to memory, and say so
        _log(_for_owner("[magic-link] cannot open %s (%s): sign-in state is kept in memory",
                        "[magic-link] не открывается %s (%s): состояние входа хранится в памяти") % (path, _describe(e)))
        return _MemoryStore()


class _Gate:
    """Logic shared by the WSGI and ASGI versions: who may get in, cookies, emails, sign-on, pages, logs."""

    def __init__(self, tokens, request_url, cookie_name, max_age, emails=None, secret=None, gateway_key=None):
        self.gateway_key = os.environ.get("SMALL_GATEWAY_KEY", "").strip() if gateway_key is None else gateway_key
        self.entries = parse_tokens(os.environ.get("ACCESS_TOKEN", "") if tokens is None else tokens)
        self.cookies = [(name, _cookie_value(token)) for name, token in self.entries]
        self.emails = parse_emails(os.environ.get("ACCESS_EMAILS", "") if emails is None else emails)
        secret = os.environ.get("SESSION_SECRET", "").strip() if secret is None else secret
        if self.emails and not secret and not self.gateway_key:
            _log(_for_owner("[magic-link] SESSION_SECRET is not set: everyone will have to sign in again after a restart",
                            "[magic-link] SESSION_SECRET не задан: после перезапуска приложения всем придётся войти заново"))
        self.secret = (secret or secrets.token_hex(32)).encode("utf-8")
        self.request_url = os.environ.get("ACCESS_REQUEST_URL", "") if request_url is None else request_url
        self.cookie_name = cookie_name
        self.state_cookie_name = cookie_name + "_sso"
        self.telegram = (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip())
        self.lang = os.environ.get("ACCESS_LANG", "auto").strip().lower()
        self.max_age = max_age
        self.sso = sso_providers()
        self.revoked = parse_revoked(os.environ.get("SESSIONS_REVOKED", ""))
        self.store = open_store()
        if self.gateway_key:
            _log(_for_owner("[magic-link] behind the small gateway: only requests it signed get in",
                            "[magic-link] за шлюзом small: пускаются только запросы, подписанные шлюзом"))
        elif not self.entries and not self.emails:
            _log(_for_owner("[magic-link] neither ACCESS_TOKEN nor ACCESS_EMAILS is set: nobody can get in",
                            "[magic-link] ACCESS_TOKEN и ACCESS_EMAILS не заданы: доступ закрыт для всех"))
        if os.environ.get("MICROSOFT_CLIENT_ID", "").strip() and "microsoft" not in self.sso and not self.gateway_key:
            _log(_for_owner("[magic-link] sign-in with Microsoft is off: it needs MICROSOFT_CLIENT_SECRET and "
                            "MICROSOFT_TENANT_ID (a GUID). Set it up: small sso microsoft",
                            "[magic-link] вход через Microsoft выключен: нужны MICROSOFT_CLIENT_SECRET и "
                            "MICROSOFT_TENANT_ID (GUID). Настроить: small sso microsoft"))
        if self.emails and not _mail_mode() and not self.sso and not self.gateway_key:
            _log(_for_owner("[magic-link] email is not set up (RESEND_API_KEY or SMTP_URL): sign-in emails will not be "
                            "sent. Set it up: small mail",
                            "[magic-link] почта не настроена (RESEND_API_KEY или SMTP_URL): письма для входа не уйдут. "
                            "Подключить: small mail"))
        if self.emails and self.store.persistent:
            _log(_for_owner("[magic-link] sign-in state is kept in %s", "[magic-link] состояние входа хранится в %s")
                 % self.store.path)

    # --- shared handler: _Pass means pass the request to the app, otherwise (status, headers, body) ---

    def wants_body(self, method, path):
        return method == "POST" and ((path == REQUEST_PATH and not self.request_url)
                                     or (bool(self.emails) and path in (LOGIN_PATH, VERIFY_PATH, LOGOUT_PATH)))

    def is_slow(self, path):
        """Requests that wait for the network (the sign-on code exchange): ASGI runs them in a thread."""
        return path == SSO_CALLBACK

    def dispatch(self, method, base, path, path_bytes, query, cookie, ip, user_agent, host, https, body=b"",
                 accept_language="", gateway=("", "", "")):
        lang = pick_lang(accept_language, self.lang)
        if self.gateway_key:  # behind the gateway it has checked everything: only its signature counts here
            who = gateway_identity(self.gateway_key, *gateway)
            return _Pass(*who) if who else self.respond(403, "gateway", base, lang=lang)
        if path == REQUEST_PATH and method == "POST" and not self.request_url:
            self.log_request(body, ip, user_agent, host)
            return self.respond(200, "sent", base, lang=lang)
        if self.emails and path == LOGIN_PATH and method == "POST":
            return self.login_request(body, ip, user_agent, host, https, base, lang)
        if self.emails and path == VERIFY_PATH:
            return self.verify(method, query, body, ip, user_agent, https, base, lang)
        if self.emails and self.sso and path == SSO_CALLBACK:
            return self.sso_callback(query, cookie, ip, user_agent, host, https, base, lang)
        if self.emails and self.sso and path.startswith(SSO_PATH) and method in ("GET", "HEAD"):
            return self.sso_redirect(path[len(SSO_PATH):], host, https, base, lang)
        if path == LOGOUT_PATH:
            return self.logout(method, cookie, body, ip, user_agent, https, base, lang)

        token = next((value for key, value in query if key == "token"), None)
        if token is not None:
            name = self.token_owner(token)
            if name is None:
                return self.respond(403, "invalid", base, lang=lang)
            self.audit("sign_in", "link", name, ip, user_agent)
            return 302, [("Location", self.location(path_bytes, query)), ("Set-Cookie", self.set_cookie(token, https)),
                         ("Cache-Control", "no-store")], b""

        who = self.identity(cookie)
        if who is not None:
            return _Pass(*who)
        return self.respond(403, "forbidden", base, lang=lang)

    def respond(self, status, kind, base, headers=(), lang=DEFAULT_LANG, **page):
        return status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
                        ("Referrer-Policy", "no-referrer"), ("Content-Language", lang), ("X-Frame-Options", "DENY"),
                        ("Content-Security-Policy", PAGE_CSP)] + list(headers), \
            self.page(kind, base, lang=lang, **page)

    # --- invitation links (ACCESS_TOKEN) ---

    def token_owner(self, token):
        """Name of the token's owner ("" if unnamed), or None if the token is wrong."""
        return _owner(token, self.entries)

    def cookie_owner(self, cookie_header):
        """Who is signed in: a name from ACCESS_TOKEN ("" if unnamed), an email address, or None."""
        return self.session_owner(self.read_cookie(cookie_header, self.cookie_name))

    def identity(self, cookie_header):
        """Who is signed in and how: (address or ACCESS_TOKEN name, "email" / "google" / "microsoft" / "link"),
        or None."""
        value = self.read_cookie(cookie_header, self.cookie_name)
        if isinstance(value, str) and value.startswith("e."):
            session = self._session(value)
            if session is None or not self.email_allowed(session[0]):
                return None
            return session
        name = _owner(value, self.cookies) if value is not None else None
        return None if name is None else (name, "link")

    def connection_identity(self, cookie_header, gateway=("", "", "")):
        """The same for a websocket: behind the gateway, its signature; otherwise the session cookie."""
        if self.gateway_key:
            return gateway_identity(self.gateway_key, *gateway)
        return self.identity(cookie_header)

    @staticmethod
    def read_cookie(cookie_header, name):
        try:
            morsel = SimpleCookie(cookie_header or "").get(name)
        except CookieError:
            return None
        return None if morsel is None else morsel.value

    def session_owner(self, value):
        """Same, by cookie value: "e.<signed>" is a session of an address, anything else is derived from a token."""
        value = value if isinstance(value, str) else ""
        if value.startswith("e."):
            email = self.session_email(value)
            return email if email is not None and self.email_allowed(email) else None
        return _owner(value, self.cookies)

    def session_email(self, value):
        """The address of a valid session "e.<signed>" (not expired, not signed out), whether or not it still has
        access; otherwise None."""
        session = self._session(value)
        return session[0] if session else None

    def _session(self, value):
        """(address, sign-in method) of a valid session "e.<signed>", whether or not it still has access; or None."""
        data = self.unsign("session", value[2:]) if isinstance(value, str) and value.startswith("e.") else None
        email = data.get("e") if data else None
        if not (isinstance(email, str) and email):
            return None
        # Sessions made before 0.8 don't say when they were issued: their expiry minus the lifetime does.
        issued = data.get("i")
        issued = issued if isinstance(issued, (int, float)) and not isinstance(issued, bool) else data["x"] - self.max_age
        if issued <= self.signed_out_at(email):
            return None
        method = data.get("m")
        return email, method if method in ("email", "google", "microsoft") else "email"

    def signed_out_at(self, email):
        """Sessions of this address issued at or before this Unix time are signed out (SESSIONS_REVOKED, the
        "Sign out on all devices" button). Issue times have milliseconds: signing in right after signing out works."""
        email = email.lower()
        revoked = self.revoked
        return max(revoked.get(email, 0), revoked.get("@" + email.rpartition("@")[2], 0), revoked.get("*", 0),
                   self.store.signed_out(email))

    def new_session(self, email, method):
        now = time.time()
        # Milliseconds, rounded down: a sign-out a moment later is always later than the issue time.
        issued = int(now * 1000) / 1000
        return "e." + self.sign("session", {"e": email, "i": issued, "x": int(now) + self.max_age, "m": method})

    def set_cookie(self, token, https):
        return self._cookie(_cookie_value(token), https)

    def _cookie(self, value, https, name=None, max_age=None):
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (
            name or self.cookie_name, value, self.max_age if max_age is None else max_age, "; Secure" if https else "")

    @staticmethod
    def location(path_bytes, query):
        # Strip the token from the URL so it doesn't stay in browser history or leak through Referer.
        # Collapse leading slashes: a Location of "//host" would be an open redirect.
        location = "/" + quote(path_bytes, safe="/").lstrip("/")
        rest = urlencode([(k, v) for k, v in query if k != "token"])
        return location + ("?" + rest if rest else "")

    # --- email sign-in (ACCESS_EMAILS) ---

    def email_allowed(self, email):
        email = email.lower()
        return email in self.emails or "@" + email.rpartition("@")[2] in self.emails

    def email_form(self):
        """Whether the page offers sign-in by email: yes, unless single sign-on is set up and email is not."""
        return bool(self.emails) and (bool(_mail_mode()) or not self.sso)

    def sign(self, purpose, data):
        payload = _b64(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        return payload + "." + self._mac(purpose, payload)

    def unsign(self, purpose, value):
        """Data from a signed string if the signature is valid and it hasn't expired; otherwise None."""
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
        # purpose keeps sign-in links, sessions and the rest apart: one can't be passed off as another.
        return _b64(hmac.new(self.secret, (purpose + "|" + payload).encode("utf-8"), hashlib.sha256).digest())

    def login_request(self, body, ip, user_agent, host, https, base, lang=DEFAULT_LANG):
        email = dict(parse_qsl(body.decode("utf-8", "replace"))).get("email", "")
        kind = self.start_login(email, ip, user_agent, host, https, base, lang, VERIFY_PATH)
        return self.respond({"bad_email": 400, "slow": 429}.get(kind, 200), kind, base, lang=lang)

    def start_login(self, email, ip, user_agent, host, https, base, lang, path):
        """Email a sign-in link to the page at path. Result: "check" (the same for known and unknown addresses;
        an unknown one is an access request), "bad_email" or "slow" (email limit exceeded)."""
        email = (email if isinstance(email, str) else "").strip().lower()[:254]
        if not EMAIL_RE.match(email):
            return "bad_email"
        if not (self.store.allow("ip:" + ip, LIMIT_PER_IP, LIMIT_WINDOW)
                and self.store.allow("mail:" + email, LIMIT_PER_EMAIL, LIMIT_WINDOW)):
            return "slow"
        if self.email_allowed(email):
            app_url = self.public_url(host, https, base)
            if app_url is None:
                _log(_for_owner("[magic-link] sign-in email not sent: the app's address is unknown. Set PUBLIC_URL",
                                "[magic-link] письмо для входа не отправлено: неизвестен адрес приложения. Задайте PUBLIC_URL"))
            else:
                link = self.login_link(app_url + path, email)
                # In the background, so the response doesn't wait for mail and looks the same for unknown addresses.
                threading.Thread(target=_send_login_mail, args=(email, link, urlsplit(app_url).netloc, lang),
                                 daemon=True).start()
        else:
            self.notify_request(email, ip, user_agent, host)
        return "check"

    def login_link(self, url, email):
        """One-time sign-in link, valid for LINK_TTL seconds."""
        return url + "?" + urlencode({"t": self.sign("login", {
            "e": email, "x": int(time.time()) + LINK_TTL, "n": secrets.token_urlsafe(9)})})

    def login_data(self, token):
        """Data of a valid sign-in link (signature valid, not expired, address on the list, not used yet)."""
        data = self.unsign("login", token if isinstance(token, str) else "")
        email = data.get("e") if data else None
        if not (isinstance(email, str) and email and self.email_allowed(email)
                and isinstance(data.get("n"), str) and not self.store.is_used("link:" + data["n"])):
            return None
        return data

    def use_login(self, token, ip, user_agent=""):
        """Use up a sign-in link: the session cookie value "e.<signed>", or None if the link is no good."""
        data = self.login_data(token)
        if data is None or not self.store.use_once("link:" + data["n"], data["x"]):
            return None
        self.audit("sign_in", "email", data["e"], ip, user_agent)
        return self.new_session(data["e"], "email")

    def verify(self, method, query, body, ip, user_agent, https, base, lang=DEFAULT_LANG):
        source = parse_qsl(body.decode("utf-8", "replace")) if method == "POST" else query
        token = dict(source).get("t", "")
        data = self.login_data(token)
        if data is None:
            return self.respond(403, "expired", base, lang=lang)
        if method != "POST":
            # Mail scanners open emailed links on their own. Sign in only by button, or a scanner would use up the link.
            return self.respond(200, "confirm", base, lang=lang, email=data["e"], token=token)
        session = self.use_login(token, ip, user_agent)
        if session is None:
            return self.respond(403, "expired", base, lang=lang)
        return 302, [("Location", (base or "") + "/"), ("Set-Cookie", self._cookie(session, https)),
                     ("Cache-Control", "no-store")], b""

    def public_url(self, host, https, base):
        """App address for the link in the email and for sign-on redirects. The Host header is trusted only locally:
        otherwise an attacker could substitute their own domain, and the sign-in link from a genuine email would go
        to them."""
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

    # --- single sign-on: Google and Microsoft (OpenID Connect, authorization code flow with PKCE) ---

    def sso_links(self, redirect_uri, providers=None):
        """({provider: authorization URL}, state cookie value, nonce) to start single sign-on.

        The state that comes back in the redirect is "<provider>.<random>"; the random part must match the signed
        state cookie, which ties the answer to the browser that started the sign-in (no login CSRF). The nonce and
        the PKCE verifier are derived from the state with SESSION_SECRET, so the cookie doesn't carry them."""
        nonce = secrets.token_urlsafe(24)
        links = {}
        for provider_id in providers or self.sso:
            provider = self.sso[provider_id]
            state = provider_id + "." + nonce
            params = {"client_id": provider["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
                      "scope": provider["scope"], "state": state, "nonce": self._derive("sso-nonce", state),
                      "code_challenge": _b64(hashlib.sha256(self._derive("sso-pkce", state).encode("ascii")).digest()),
                      "code_challenge_method": "S256", "prompt": "select_account"}
            if provider_id == "microsoft":
                params["response_mode"] = "query"
            links[provider_id] = provider["auth_url"] + "?" + urlencode(params)
        cookie = self.sign("sso", {"s": nonce, "r": redirect_uri, "x": int(time.time()) + SSO_TTL})
        return links, cookie, nonce

    def _derive(self, purpose, state):
        return _b64(hmac.new(self.secret, ("%s|%s" % (purpose, state)).encode("utf-8"), hashlib.sha256).digest())

    def sso_finish(self, params, state_cookie, ip, user_agent, host):
        """Finish single sign-on with the redirect's parameters (code, state, error).

        Result: (kind, session, email): ("session", cookie value, address) on success; otherwise session is None and
        kind is the page to show: "denied" (the address has no access; the owner gets an access request),
        "unverified" (the provider doesn't vouch for the address) or "sso_failed" (anything else; the reason is
        logged)."""
        params = params if isinstance(params, dict) else {}
        provider_id, _, nonce = str(params.get("state") or "").partition(".")
        data = self.unsign("sso", state_cookie or "")
        if not (data and provider_id in self.sso
                and hmac.compare_digest(str(data.get("s", "")).encode("utf-8"), nonce.encode("utf-8"))):
            return self._sso_failed("the sign-in was started in another browser or has expired")
        if params.get("error"):
            return self._sso_failed("the provider answered: %s" % str(params["error"])[:100])
        if not params.get("code"):
            return self._sso_failed("the provider sent no code")
        provider, state = self.sso[provider_id], provider_id + "." + nonce
        try:
            claims = _exchange_code(provider, str(params["code"]), data.get("r", ""), self._derive("sso-pkce", state))
            email = _identity(provider, claims, self._derive("sso-nonce", state))
        except SsoError as e:
            if e.unverified:
                _log(_for_owner("[magic-link] %s sign-in refused: %s", "[magic-link] вход через %s отклонён: %s")
                     % (provider_id, e))
                return "unverified", None, e.email
            return self._sso_failed(str(e))
        if not self.email_allowed(email):
            self.audit("denied", provider_id, email, ip, user_agent)
            if self.store.allow("ip:" + ip, LIMIT_PER_IP, LIMIT_WINDOW):
                self.notify_request(email, ip, user_agent, host)
            return "denied", None, email
        if not self.store.use_once("sso:" + nonce, data["x"]):  # the same answer twice: a replay
            return self._sso_failed("this sign-in has already been used")
        self.audit("sign_in", provider_id, email, ip, user_agent)
        return "session", self.new_session(email, provider_id), email

    @staticmethod
    def _sso_failed(reason):
        _log(_for_owner("[magic-link] single sign-on failed: %s", "[magic-link] вход через провайдера не удался: %s")
             % reason)
        return "sso_failed", None, ""

    def sso_redirect(self, provider_id, host, https, base, lang):
        if provider_id not in self.sso:
            return self.respond(400, "sso_failed", base, lang=lang)
        app_url = self.public_url(host, https, base)
        if app_url is None:
            _log(_for_owner("[magic-link] single sign-on can't start: the app's address is unknown. Set PUBLIC_URL",
                            "[magic-link] вход через провайдера не начат: неизвестен адрес приложения. Задайте PUBLIC_URL"))
            return self.respond(500, "sso_failed", base, lang=lang)
        links, cookie, _ = self.sso_links(app_url + SSO_CALLBACK, [provider_id])
        return 302, [("Location", links[provider_id]),
                     ("Set-Cookie", self._cookie(cookie, https, self.state_cookie_name, SSO_TTL)),
                     ("Cache-Control", "no-store"), ("Referrer-Policy", "no-referrer")], b""

    def sso_callback(self, query, cookie, ip, user_agent, host, https, base, lang):
        kind, session, email = self.sso_finish(dict(query), self.read_cookie(cookie, self.state_cookie_name),
                                               ip, user_agent, host)
        clear = ("Set-Cookie", self._cookie("", https, self.state_cookie_name, 0))
        if session is None:
            return self.respond(403 if kind in ("denied", "unverified") else 400, kind, base, [clear], lang=lang,
                                email=email)
        return 302, [("Location", (base or "") + "/"), clear, ("Set-Cookie", self._cookie(session, https)),
                     ("Cache-Control", "no-store")], b""

    # --- signing out ---

    def everywhere_token(self, session):
        """For a valid session: a short-lived token for the "Sign out on all devices" button; otherwise None.
        Only when the state is persistent: kept in memory, a sign-out would be forgotten at the next restart."""
        email = self.session_email(session)
        if email is None or not self.store.persistent:
            return None
        return self.sign("everywhere", {"e": email, "x": int(time.time()) + SSO_TTL})

    def sign_out_everywhere(self, token, ip, user_agent=""):
        """End every session of the address behind an everywhere_token: the address, or None if the token is bad."""
        data = self.unsign("everywhere", token if isinstance(token, str) else "")
        email = data.get("e") if data else None
        if not (isinstance(email, str) and email):
            return None
        self.store.sign_out(email, time.time())
        self.audit("sign_out_all", "email", email, ip, user_agent)
        return email

    def logout(self, method, cookie, body, ip, user_agent, https, base, lang):
        clear = [("Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % self.cookie_name)]
        if method == "POST" and self.emails:
            email = self.sign_out_everywhere(dict(parse_qsl(body.decode("utf-8", "replace"))).get("t", ""), ip,
                                             user_agent)
            if email is not None:
                return self.respond(200, "logout_all", base, clear, lang=lang, email=email)
        if not self.emails:
            return self.respond(200, "forbidden", base, clear, lang=lang)
        everywhere = self.everywhere_token(self.read_cookie(cookie, self.cookie_name))
        return self.respond(200, "logout", base, clear, lang=lang, token=everywhere or "")

    # --- pages and logging ---

    def page(self, kind, base, email="", token="", lang=DEFAULT_LANG):
        if kind == "forbidden" and self.emails:
            kind = "sso" if self.sso else "login"
        icon, title, text = MESSAGES[lang][kind]
        ui = UI[lang]
        if "%s" in text:
            text = text % email
        if kind == "sent":
            action = ""
        elif kind == "check":
            action = '<a class="link" href="%s/">%s</a>' % (html.escape(base), ui["other"])
        elif kind == "confirm":
            action = CONFIRM_FORM.substitute(action=html.escape(base + VERIFY_PATH), token=html.escape(token),
                                             button=ui["sign_in"])
        elif kind in ("denied", "unverified"):
            action = '<a class="link" href="%s/">%s</a>' % (html.escape(base), ui["other_account"])
        elif self.emails:
            action = self.sign_in_options(base, ui)
            if kind == "logout" and token:
                action = EVERYWHERE_FORM.substitute(action=html.escape(base + LOGOUT_PATH), token=html.escape(token),
                                                    button=ui["everywhere"]) + action
        elif self.request_url:
            action = '<a class="btn" href="%s">%s</a>' % (html.escape(self.request_url), ui["request"])
        else:
            action = REQUEST_FORM.substitute(action=html.escape(base + REQUEST_PATH), placeholder=ui["contact"],
                                             button=ui["request"])
        return PAGE.substitute(lang=lang, icon=icon, title=html.escape(title), text=html.escape(text),
                               action=action).encode("utf-8")

    def sign_in_options(self, base, ui):
        """Single sign-on buttons, then the email form (if email is set up or there is no single sign-on)."""
        buttons = "".join(SSO_BUTTON.substitute(href=html.escape(base + SSO_PATH + provider_id), logo=LOGOS[provider_id],
                                                label=ui[provider_id]) for provider_id in self.sso)
        if not self.email_form():
            return buttons
        form = LOGIN_FORM.substitute(action=html.escape(base + LOGIN_PATH), button=ui["get_link"])
        return buttons + ('<div class="or">%s</div>' % ui["or"] if buttons else "") + form

    def log_request(self, body, ip, user_agent, host=""):
        contact = dict(parse_qsl(body.decode("utf-8", "replace"))).get("contact", "")[:200]
        self.notify_request(contact, ip, user_agent, host)

    def notify_request(self, contact, ip, user_agent, host=""):
        # repr() shows a newline in the contact as \n; JSON keeps the entry on one log line anyway.
        _log_event("[access-request] contact=%r ip=%s ua=%r" % (contact, ip, user_agent[:200]), "warn",
                   magic_link="request", user=contact, ip=ip, ua=user_agent[:200])
        token, chat = self.telegram
        if token and chat:
            hint = "small access add " + (contact if EMAIL_RE.match(contact) else _for_owner("NAME", "ИМЯ"))
            text = _for_owner("🔑 Access request to %s\nContact: %s\nIP: %s\n\nGrant access: %s",
                              "🔑 Запрос доступа к %s\nКонтакт: %s\nIP: %s\n\nВыдать доступ: %s") % (
                host or _for_owner("the app", "приложению"), contact or "—", ip or "—", hint)
            # In the background: the user doesn't wait for Telegram. No parse_mode, so form text can't add markup.
            threading.Thread(target=_send_telegram, args=(token, chat, text), daemon=True).start()

    @staticmethod
    def audit(event, method, who, ip, user_agent=""):
        """One JSON line per sign-in, refusal or sign-out: Railway turns the fields into log attributes, and
        `small access log` collects the lines with @magic_link:* across deploys."""
        en, ru = AUDIT[event][method]
        text = _for_owner(en, ru)
        _log_event("[magic-link] %s%s, ip %s" % (text, ": " + who if who else "", ip or "—"),
                   "warn" if event == "denied" else "info",
                   magic_link=event, method=method, user=who, ip=ip, ua=(user_agent or "")[:200])


class MagicLink:
    """WSGI middleware. A request that gets through carries environ["magic_link.user"] and ["magic_link.method"]."""

    def __init__(self, app, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE,
                 emails=None, secret=None, gateway_key=None):
        self.app = app
        self.gate = _Gate(tokens, request_url, cookie_name, max_age, emails, secret, gateway_key)

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
            body, environ.get("HTTP_ACCEPT_LANGUAGE", ""),
            tuple(environ.get("HTTP_" + name.upper().replace("-", "_"), "") for name in GATEWAY_HEADERS))
        if isinstance(result, _Pass):
            if not gate.gateway_key:  # nobody else may claim the gateway's headers
                for name in GATEWAY_HEADERS:
                    environ.pop("HTTP_" + name.upper().replace("-", "_"), None)
            environ["magic_link.user"], environ["magic_link.method"] = result.user, result.method
            return self.app(environ, start_response)
        status, headers, payload = result
        start_response(STATUS[status], headers + [("Content-Length", str(len(payload)))])
        return [payload]


class MagicLinkASGI:
    """ASGI middleware (FastAPI, Starlette, Litestar...). A websocket without access is closed before accept (403).
    A request that gets through carries scope["magic_link.user"] and scope["magic_link.method"]."""

    def __init__(self, app, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE,
                 emails=None, secret=None, gateway_key=None):
        self.app = app
        self.gate = _Gate(tokens, request_url, cookie_name, max_age, emails, secret, gateway_key)

    def _let_in(self, scope, user, method):
        """The scope the app gets: who is signed in, and without the gateway's headers unless they were checked."""
        scope = dict(scope, **{"magic_link.user": user, "magic_link.method": method})
        if not self.gate.gateway_key:
            scope["headers"] = [(k, v) for k, v in scope.get("headers") or []
                                if k.decode("latin-1").lower() not in GATEWAY_HEADERS]
        return scope

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):  # lifespan and the rest: no check
            return await self.app(scope, receive, send)
        gate = self.gate
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        gateway = tuple(headers.get(name, "") for name in GATEWAY_HEADERS)
        if scope["type"] == "websocket":
            who = gate.connection_identity(headers.get("cookie", ""), gateway)
            if who is not None:
                return await self.app(self._let_in(scope, *who), receive, send)
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
        handle = functools.partial(
            gate.dispatch, method, base, path, (base + path).encode("utf-8"),
            parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True),
            headers.get("cookie", ""), _client_ip(headers.get("x-forwarded-for"), client[0]),
            headers.get("user-agent", ""), headers.get("host", ""),
            _is_https(headers.get("x-forwarded-proto"), scope.get("scheme")), body[:4096],
            headers.get("accept-language", ""), gateway)
        # The sign-on callback waits for the provider: in a thread, so the event loop keeps serving others.
        result = await asyncio.get_running_loop().run_in_executor(None, handle) if gate.is_slow(path) else handle()
        if isinstance(result, _Pass):
            return await self.app(self._let_in(scope, result.user, result.method), receive, send)
        status, response_headers, payload = result
        await send({"type": "http.response.start", "status": status, "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in response_headers
        ] + [(b"content-length", str(len(payload)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": payload})


class MagicLinkGate:
    """The same protection for frameworks that can't be wrapped in middleware (e.g. Streamlit).

    Same environment variables, signatures, one-time links, email limits, sign-on, logging and texts as MagicLink, but
    no HTTP: the app shows the forms itself, and it sets and reads the cookies (gate.cookie_name, and
    the single sign-on state cookie) itself. Create one object per process: it holds the sign-in state.

        gate = MagicLinkGate()
        gate.session_owner(cookie_value)       # who is signed in, by cookie value: address, ACCESS_TOKEN name or None
        gate.token_login(token, headers)       # ?token=: name ("" for a shared link) or None
        gate.start_login(email, headers)       # emails a ?t= link: "check" for any address, "bad_email", "slow"
        gate.login_email(t)                    # who the ?t= link was issued to (doesn't use it up), or None
        gate.finish_login(t, headers)          # only on a button press: uses up the link; cookie value or None
        gate.sso_links(headers)                # single sign-on: ({provider: URL}, cookie name, value) or None
        gate.sso_finish(params, cookies, headers)  # the redirect back: (kind, cookie value or None, address)
        gate.everywhere_token(cookie_value)    # for a "Sign out on all devices" button, or None
        gate.sign_out_everywhere(token, headers)  # ends every session of that address
        gate.request_access(contact, headers)  # access request: to the log and to Telegram
        gate.gateway_user(headers)             # behind the small gateway: who it let in, or None
    headers: the request headers (any Mapping, case-insensitive); IP, Host, User-Agent and language come from them.
    """

    def __init__(self, tokens=None, request_url=None, cookie_name=COOKIE_NAME, max_age=COOKIE_MAX_AGE,
                 emails=None, secret=None, gateway_key=None):
        self._gate = _Gate(tokens, request_url, cookie_name, max_age, emails, secret, gateway_key)
        self.cookie_name, self.max_age = cookie_name, max_age
        self.state_cookie_name = self._gate.state_cookie_name

    @property
    def emails(self):
        """Addresses and domains from ACCESS_EMAILS. Empty means no sign-in by address (email or sign-on)."""
        return frozenset(self._gate.emails)

    @property
    def behind_gateway(self):
        """Whether the app is behind the small gateway (SMALL_GATEWAY_KEY): then use gateway_user, not the forms."""
        return bool(self._gate.gateway_key)

    def gateway_user(self, headers=None):
        """Behind the small gateway: who it let in (address or ACCESS_TOKEN name, "" for the shared link), if the
        request carries its valid signature; otherwise None. Without the gateway: always None."""
        who = gateway_identity(self._gate.gateway_key, *(_header(headers, name) for name in GATEWAY_HEADERS))
        return who[0] if who else None

    @property
    def request_url(self):
        """ACCESS_REQUEST_URL: where the "Request access" button leads; "" means the built-in request form."""
        return self._gate.request_url

    @property
    def sso(self):
        """Single sign-on providers that are set up, in button order: ("google", "microsoft") or fewer."""
        return tuple(self._gate.sso) if self._gate.emails else ()

    @property
    def email_form(self):
        """Whether to offer sign-in by email (it is hidden when there is single sign-on but no email set up)."""
        return self._gate.email_form()

    @property
    def persistent(self):
        """Whether sign-in state survives a restart (ACCESS_DB or a Railway volume)."""
        return self._gate.store.persistent

    def lang(self, headers=None):
        """Visitor's language: ACCESS_LANG, otherwise from Accept-Language; "ru" or "en"."""
        return pick_lang(_header(headers, "accept-language"), self._gate.lang)

    @staticmethod
    def text(kind, lang, email=""):
        """(icon, title, text) from MESSAGES as plain text, without HTML entities."""
        icon, title, text = MESSAGES[lang if lang in MESSAGES else DEFAULT_LANG][kind]
        return html.unescape(icon), title, (text % email if "%s" in text else text)

    @staticmethod
    def label(name, lang):
        """Button or field label from UI: "request", "contact", "get_link", "sign_in", "other", "google",
        "microsoft", "or", "everywhere", "other_account"."""
        return UI[lang if lang in UI else DEFAULT_LANG][name]

    def token_login(self, token, headers=None):
        """Sign in with an invitation link ?token=: the ACCESS_TOKEN name ("" if unnamed) or None. Logs the sign-in."""
        name = self._gate.token_owner(token if isinstance(token, str) else "")
        if name is not None:
            ip, user_agent, _, _ = _request_info(headers)
            self._gate.audit("sign_in", "link", name, ip, user_agent)
        return name

    def session_owner(self, value):
        """Who is signed in, by the value of the gate.cookie_name cookie: email address, ACCESS_TOKEN name or None.
        Removing the address from ACCESS_EMAILS, signing it out or changing SESSION_SECRET invalidates the session."""
        return self._gate.session_owner(value)

    def start_login(self, email, headers=None, path="/"):
        """Send an email with the link <app address><path>?t=... The address comes from PUBLIC_URL or
        RAILWAY_PUBLIC_DOMAIN; the Host header is trusted only for localhost. Result: "check" (the same for known
        and unknown addresses; an unknown one becomes an access request), "bad_email" or "slow"."""
        ip, user_agent, host, https = _request_info(headers)
        return self._gate.start_login(email, ip, user_agent, host, https, "", self.lang(headers), path)

    def login_link(self, email, app_url, path="/"):
        """A sign-in link without the email (for tests and custom delivery): <app_url><path>?t=..."""
        return self._gate.login_link(app_url.rstrip("/") + path, email.strip().lower())

    def login_email(self, token):
        """Address from a valid sign-in link, or None. Doesn't use up the link: mail scanners open it too,
        so sign-in happens only on a button press (finish_login)."""
        data = self._gate.login_data(token)
        return data["e"] if data else None

    def finish_login(self, token, headers=None):
        """Use up the sign-in link (once, under a lock): the session cookie value or None. Logs the sign-in."""
        ip, user_agent, _, _ = _request_info(headers)
        return self._gate.use_login(token, ip, user_agent)

    def sso_links(self, headers=None, path="/"):
        """Start single sign-on: ({provider: authorization URL}, state cookie name, state cookie value), or None if
        there is no sign-on or the app's address is unknown. The provider sends the visitor back to
        <app address><path>?code=...&state=...; register that address with the provider. Set the cookie (for
        SSO_TTL seconds) before the visitor follows a link: it proves the answer comes back to the same browser.
        Each call gets its own cookie name, so sign-in screens open in several tabs don't overwrite each other."""
        if not self.sso:
            return None
        _, _, host, https = _request_info(headers)
        app_url = self._gate.public_url(host, https, "")
        if app_url is None:
            return None
        links, value, nonce = self._gate.sso_links(app_url + path)
        return links, self._state_cookie(nonce), value

    def _state_cookie(self, nonce):
        return "%s_%s" % (self.state_cookie_name, nonce[:12])

    def sso_finish(self, params, cookies, headers=None):
        """Finish single sign-on with the query parameters of the redirect back (code, state, error) and the request
        cookies (a Mapping; the state cookie is found by its name). Result: (kind, session cookie value or None,
        address); kind is "session" on success, otherwise the screen to show: "denied", "unverified" or
        "sso_failed". Logs the sign-in or the refusal."""
        nonce = str((params or {}).get("state") or "").partition(".")[2]
        value = (cookies or {}).get(self._state_cookie(nonce)) if nonce else None
        ip, user_agent, host, _ = _request_info(headers)
        return self._gate.sso_finish(params, value if isinstance(value, str) else None, ip, user_agent, host)

    @staticmethod
    def is_sso_return(params):
        """Whether these query parameters are a provider sending the visitor back (state "google.…"/"microsoft.…")."""
        state = str((params or {}).get("state") or "")
        return state.partition(".")[0] in ("google", "microsoft") and bool(state.partition(".")[2])

    def everywhere_token(self, value):
        """Token for a "Sign out on all devices" button for the session with this cookie value, or None (no valid
        session, or the state is kept in memory, where the sign-out would not survive a restart)."""
        return self._gate.everywhere_token(value)

    def sign_out_everywhere(self, token, headers=None):
        """End every session of the address behind the token, on all devices: the address, or None."""
        ip, user_agent, _, _ = _request_info(headers)
        return self._gate.sign_out_everywhere(token, ip, user_agent)

    def request_access(self, contact, headers=None):
        """Access request: an [access-request] log line (one line whatever the contact holds) and a Telegram
        message."""
        ip, user_agent, host, _ = _request_info(headers)
        self._gate.notify_request((contact if isinstance(contact, str) else "").strip()[:200], ip, user_agent, host)


def _header(headers, name):
    for key, value in (headers or {}).items():
        if str(key).lower() == name and isinstance(value, str):
            return value
    return ""


def _request_info(headers):
    """(ip, user_agent, host, https) from the request headers, for MagicLinkGate."""
    return (_client_ip(_header(headers, "x-forwarded-for"), ""), _header(headers, "user-agent"),
            _header(headers, "host"), _is_https(_header(headers, "x-forwarded-proto"), None))


def _owner(value, entries):
    value = value.encode("utf-8")
    for name, candidate in entries:
        if hmac.compare_digest(value, candidate.encode("utf-8")):
            return name
    return None


def _cookie_value(token):
    # The cookie holds a value derived from the token, not the token itself: remove the token from ACCESS_TOKEN
    # and its cookies stop working.
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
    """How to send sign-in emails: "log", "resend", "smtp" or "" (not set up)."""
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
    except Exception as e:  # a failed email is no reason to crash the app; keys and passwords stay out of the log
        _log(_for_owner("[magic-link] could not send the sign-in email to %s: %s",
                        "[magic-link] не удалось отправить письмо для входа на %s: %s") % (email, _describe(e)))


def send_mail(to, subject, text, html_body):
    """Send an email through Resend (RESEND_API_KEY) or SMTP (SMTP_URL). Raises on failure."""
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
    except Exception as e:  # a failed notification is no reason to fail the request; the bot token stays out of the log
        _log(_for_owner("[magic-link] could not send the access request to Telegram: %s",
                        "[magic-link] не удалось отправить запрос доступа в Telegram: %s") % type(e).__name__)


def _for_owner(en, ru):
    """Text for the owner (log, Telegram) in their language, OWNER_LANG. Visitors see their browser's language."""
    return ru if os.environ.get("OWNER_LANG", "").strip().lower()[:2] == "ru" else en


def _log(message):
    print(message, flush=True)


def _log_event(message, level, **fields):
    """A log line as one JSON object: message and level for people, the rest as attributes for filters."""
    _log(json.dumps(dict({"message": message, "level": level}, **fields), ensure_ascii=False))


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

EVERYWHERE_FORM = Template(
    '<form method="post" action="$action"><input type="hidden" name="t" value="$token">'
    '<button class="btn quiet" type="submit">$button</button></form>'
)

SSO_BUTTON = Template('<a class="btn sso" href="$href">$logo<span>$label</span></a>')

LOGOS = {
    "google": '<svg viewBox="0 0 48 48" aria-hidden="true">'
              '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 '
              '5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>'
              '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 '
              '7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>'
              '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 '
              '0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>'
              '<path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 '
              '0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>',
    "microsoft": '<svg viewBox="0 0 21 21" aria-hidden="true"><path fill="#f25022" d="M1 1h9v9H1z"/>'
                 '<path fill="#7fba00" d="M11 1h9v9h-9z"/><path fill="#00a4ef" d="M1 11h9v9H1z"/>'
                 '<path fill="#ffb900" d="M11 11h9v9h-9z"/></svg>',
}

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
  .btn.sso, .btn.quiet { display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 12px;
                         color: var(--text); background: transparent; border: 1px solid var(--border); }
  .btn.sso svg { width: 18px; height: 18px; flex: none; }
  .or { display: flex; align-items: center; gap: 12px; margin: 8px 0 16px; color: var(--muted); font-size: 14px; }
  .or::before, .or::after { content: ""; flex: 1; border-top: 1px solid var(--border); }
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
