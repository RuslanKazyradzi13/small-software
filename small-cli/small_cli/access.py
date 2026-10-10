"""Доступ к приложению: ACCESS_TOKEN (ссылки-приглашения "имя:токен,…") и ACCESS_EMAILS (вход по почте).

Тот же формат понимают magic_link.py, magic-link.js и шаблон streamlit: убрали чей-то токен — его ссылка
и куки перестают работать, у остальных всё остаётся. Убрали адрес из ACCESS_EMAILS — вход по нему закрыт сразу.
"""

import re
import secrets
from urllib.parse import quote

from .config import CONFIG_FILE, load_config
from .railway import Railway, RailwayError, token_from_env
from .scaffold import TEMPLATES_DIR

NAME_RE = re.compile(r"[A-Za-z0-9_.@-]{1,40}\Z")
EMAIL_RE = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+\Z")
DOMAIN_RE = re.compile(r"@[^@\s,;<>\"']+\.[^@\s,;<>\"']+\Z")
MAIL_VARS = ("RESEND_API_KEY", "SMTP_URL", "LOGIN_LINKS_TO_LOG")


def parse(value):
    """"alice:tok1, tok2" -> [("alice", "tok1"), ("", "tok2")]."""
    entries = []
    for item in (value or "").split(","):
        name, _, token = item.strip().rpartition(":")
        if token.strip():
            entries.append((name.strip(), token.strip()))
    return entries


def serialize(entries):
    return ",".join("%s:%s" % (name, token) if name else token for name, token in entries)


def new_token():
    return secrets.token_urlsafe(32)


def is_email(value):
    """Адрес почты или целый домен "@company.com"."""
    return bool(EMAIL_RE.match(value) or DOMAIN_RE.match(value))


def parse_emails(value):
    """"Ivan@Company.com, @team.com" -> ["ivan@company.com", "@team.com"]: без регистра, повторов и мусора."""
    found = []
    for item in (value or "").split(","):
        item = item.strip().lower()
        if is_email(item) and item not in found:
            found.append(item)
    return found


def app_env(root, state, warn=None):
    """(переменные приложения, откуда каждая): Railway, поверх — непустые значения из small.json."""
    env, sources = {}, {}
    linked = all(state.get(k) for k in ("projectId", "environmentId", "serviceId"))
    if linked and token_from_env():
        try:
            env = dict(Railway(token_from_env()).service_variables(
                state["projectId"], state["environmentId"], state["serviceId"]))
            sources = dict.fromkeys(env, "Railway")
        except RailwayError as e:
            if warn:
                warn("Не удалось прочитать переменные сервиса в Railway: %s" % e)
    for name, value in load_config(root / CONFIG_FILE)["env"].items():
        if value:
            env[name], sources[name] = value, CONFIG_FILE
    return env, sources


def current(root, state, warn=None):
    """(записи ACCESS_TOKEN, откуда): из small.json, иначе из переменных сервиса в Railway, иначе ([], None)."""
    raw = load_config(root / CONFIG_FILE)["env"].get("ACCESS_TOKEN")
    if raw:
        return parse(raw), CONFIG_FILE
    env, sources = app_env(root, state, warn)
    if env.get("ACCESS_TOKEN"):
        return parse(env["ACCESS_TOKEN"]), sources["ACCESS_TOKEN"]
    return [], None


def pick(entries, name=None):
    """Токен человека name; без имени — первый (владельца). None, если такого нет."""
    if name is None:
        return entries[0][1] if entries else None
    return next((token for who, token in entries if who == name), None)


def invite_link(domain, token):
    url = "https://%s/" % domain
    return url + "?token=" + quote(token, safe="") if token else url


def email_login_problem(root):
    """None — код приложения умеет вход по почте; иначе — что мешает и как исправить."""
    for name, template in (("magic_link.py", "python-http"), ("magic-link.js", "node-http")):
        path = root / name
        if path.is_file():
            if "ACCESS_EMAILS" in path.read_text(encoding="utf-8", errors="replace"):
                return None
            return ("%s в приложении старой версии и не знает входа по почте. Замените его новым: %s"
                    % (name, TEMPLATES_DIR / template / name))
    app = root / "app.py"
    if app.is_file() and "streamlit" in app.read_text(encoding="utf-8", errors="replace"):
        return "в шаблоне streamlit вход по почте пока не поддерживается — выдайте личную ссылку: small access add ИМЯ"
    return None
