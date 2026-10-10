"""App access: ACCESS_TOKEN (invite links "name:token,…") and ACCESS_EMAILS (sign-in by email).

magic_link.py, magic-link.js and the streamlit template understand the same format: remove someone's token and
their link and cookie stop working, while everyone else keeps access. Remove an address from ACCESS_EMAILS and
sign-in with it is closed at once.
"""

import re
import secrets
from urllib.parse import quote

from .config import CONFIG_FILE, load_config
from .i18n import tr
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
    """An email address or a whole domain "@company.com"."""
    return bool(EMAIL_RE.match(value) or DOMAIN_RE.match(value))


def parse_emails(value):
    """"Ivan@Company.com, @team.com" -> ["ivan@company.com", "@team.com"]: lowercased, without duplicates or junk."""
    found = []
    for item in (value or "").split(","):
        item = item.strip().lower()
        if is_email(item) and item not in found:
            found.append(item)
    return found


def app_env(root, state, warn=None):
    """(app variables, where each one comes from): Railway, overlaid with non-empty values from small.json."""
    env, sources = {}, {}
    linked = all(state.get(k) for k in ("projectId", "environmentId", "serviceId"))
    if linked and token_from_env():
        try:
            env = dict(Railway(token_from_env()).service_variables(
                state["projectId"], state["environmentId"], state["serviceId"]))
            sources = dict.fromkeys(env, "Railway")
        except RailwayError as e:
            if warn:
                warn(tr("Could not read the service variables from Railway: %s",
                        "Не удалось прочитать переменные сервиса в Railway: %s") % e)
    for name, value in load_config(root / CONFIG_FILE)["env"].items():
        if value:
            env[name], sources[name] = value, CONFIG_FILE
    return env, sources


def current(root, state, warn=None):
    """(ACCESS_TOKEN entries, source): from small.json, else from the service variables in Railway, else ([], None)."""
    raw = load_config(root / CONFIG_FILE)["env"].get("ACCESS_TOKEN")
    if raw:
        return parse(raw), CONFIG_FILE
    env, sources = app_env(root, state, warn)
    if env.get("ACCESS_TOKEN"):
        return parse(env["ACCESS_TOKEN"]), sources["ACCESS_TOKEN"]
    return [], None


def pick(entries, name=None):
    """The token of the person `name`; without a name, the first one (the owner's). None if there is no such entry."""
    if name is None:
        return entries[0][1] if entries else None
    return next((token for who, token in entries if who == name), None)


def invite_link(domain, token):
    url = "https://%s/" % domain
    return url + "?token=" + quote(token, safe="") if token else url


def email_login_problem(root):
    """None if the app code supports sign-in by email; otherwise, what is in the way and how to fix it.

    Streamlit cannot be wrapped in middleware: there sign-in by email is done by require_access() in app.py via
    MagicLinkGate from magic_link.py, so new versions of both files are needed."""
    app = root / "app.py"
    source = app.read_text(encoding="utf-8", errors="replace") if app.is_file() else ""
    streamlit = "streamlit" in source
    if streamlit and "MagicLinkGate" not in source:
        return (tr("app.py is from an old streamlit template without sign-in by email. Take require_access() from "
                   "the new app.py, and magic_link.py next to it: %s (or grant a personal link: small access add NAME)",
                   "app.py — из старого шаблона streamlit, без входа по почте. Возьмите require_access() из нового "
                   "app.py и magic_link.py рядом с ним: %s (или выдайте личную ссылку: small access add ИМЯ)")
                % (TEMPLATES_DIR / "streamlit"))
    for name, template in (("magic_link.py", "streamlit" if streamlit else "python-http"),
                           ("magic-link.js", "node-http")):
        path = root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if "ACCESS_EMAILS" in text and (not streamlit or "MagicLinkGate" in text):
                return None
            return (tr("%s in the app is an old version and does not support sign-in by email. "
                       "Replace it with the new one: %s",
                       "%s в приложении старой версии и не знает входа по почте. Замените его новым: %s")
                    % (name, TEMPLATES_DIR / template / name))
    if streamlit:
        return (tr("magic_link.py is missing next to app.py. Copy it from the template: %s",
                   "рядом с app.py нет magic_link.py. Скопируйте его из шаблона: %s")
                % (TEMPLATES_DIR / "streamlit" / "magic_link.py"))
    return None
