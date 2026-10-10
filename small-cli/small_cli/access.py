"""App access: ACCESS_TOKEN (invite links "name:token,…") and ACCESS_EMAILS (sign-in by email).

magic_link.py, magic-link.js and the streamlit template understand the same format: remove someone's token and
their link and cookie stop working, while everyone else keeps access. Remove an address from ACCESS_EMAILS and
sign-in with it is closed at once.
"""

import re
import secrets
from urllib.parse import quote

from . import gateway
from .config import CONFIG_FILE, load_config
from .i18n import tr
from .railway import Railway, RailwayError, token_from_env
from .scaffold import TEMPLATES_DIR

NAME_RE = re.compile(r"[A-Za-z0-9_.@-]{1,40}\Z")
EMAIL_RE = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+\Z")
DOMAIN_RE = re.compile(r"@[^@\s,;<>\"']+\.[^@\s,;<>\"']+\Z")
MAIL_VARS = ("RESEND_API_KEY", "SMTP_URL", "LOGIN_LINKS_TO_LOG")
# Single sign-on: a provider is on when all of its variables are set (small sso google / small sso microsoft).
SSO_VARS = {"google": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
            "microsoft": ("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET", "MICROSOFT_TENANT_ID")}
SSO_NAMES = {"google": "Google", "microsoft": "Microsoft"}
SSO_CALLBACK = "/_access/sso/callback"
SESSION_DAYS = 30  # how long a sign-in lasts in magic_link: older sign-out times can be forgotten


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
    """(app variables, where each one comes from): Railway, overlaid with non-empty values from small.json. Behind the
    gateway, the sign-in variables come from the gateway's service."""
    env, sources = {}, {}
    linked = all(state.get(k) for k in ("projectId", "environmentId", "serviceId"))
    if linked and token_from_env():
        try:
            api = Railway(token_from_env())
            env = dict(api.service_variables(state["projectId"], state["environmentId"], state["serviceId"]))
            if gateway.is_on(state):
                for name in gateway.GATEWAY_ONLY:
                    env.pop(name, None)
                ours = api.service_variables(state["projectId"], state["environmentId"], state["gateway"]["serviceId"])
                env.update({k: v for k, v in ours.items() if k in gateway.GATEWAY_ONLY + gateway.SHARED})
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


def sso_enabled(env):
    """Single sign-on providers set up in the app's variables, in button order: ["google", "microsoft"] or fewer."""
    return [provider for provider, names in SSO_VARS.items() if all(env.get(name) for name in names)]


def is_streamlit(root):
    app = root / "app.py"
    return app.is_file() and "streamlit" in app.read_text(encoding="utf-8", errors="replace")


def app_url(root, state):
    """The app's public address: PUBLIC_URL from small.json (your own domain), else the Railway domain, else None."""
    custom = load_config(root / CONFIG_FILE)["env"].get("PUBLIC_URL", "").strip().rstrip("/")
    if custom:
        return custom
    return "https://" + state["domain"] if state.get("domain") else None


def redirect_uri(root, state):
    """Where Google and Microsoft send people back after sign-in: /_access/sso/callback, or the page itself for
    Streamlit without the gateway (it can't serve other paths). None until the app has an address."""
    base = app_url(root, state)
    if base is None:
        return None
    return base + "/" if is_streamlit(root) and not gateway.is_on(state) else base + SSO_CALLBACK


def parse_revoked(value):
    """SESSIONS_REVOKED "ivan@company.com=1760000000, *=1760000100" -> {"ivan@company.com": 1760000000, "*": ...}."""
    found = {}
    for item in (value or "").split(","):
        who, _, moment = item.strip().lower().rpartition("=")
        who = who.strip()
        if (who == "*" or is_email(who)) and moment.strip().isdigit():
            found[who] = max(found.get(who, 0), int(moment))
    return found


def add_revoked(value, who, now):
    """SESSIONS_REVOKED with `who` (an address, @domain or *) signed out at `now`. Sign-outs older than a session's
    lifetime are dropped: every session they could end has expired anyway."""
    entries = parse_revoked(value)
    entries[who.lower()] = int(now)
    keep = sorted((w, t) for w, t in entries.items() if t > now - SESSION_DAYS * 24 * 3600)
    return ", ".join("%s=%d" % pair for pair in keep)


def feature_problem(root, state=None):
    """None if the app's code knows sign-in with Google and Microsoft and signing out (small 0.8), or the gateway
    does it for the app; otherwise what is in the way and how to fix it."""
    if gateway.is_on(state or {}):
        return None
    streamlit = is_streamlit(root)
    if streamlit and "sso_finish" not in (root / "app.py").read_text(encoding="utf-8", errors="replace"):
        return (tr("app.py is from a streamlit template older than small 0.8, without sign-in with Google and "
                   "Microsoft. Take require_access() from the new app.py, and magic_link.py next to it: %s",
                   "app.py — из шаблона streamlit старше small 0.8, без входа через Google и Microsoft. Возьмите "
                   "require_access() из нового app.py и magic_link.py рядом с ним: %s") % (TEMPLATES_DIR / "streamlit"))
    for name, template in (("magic_link.py", "streamlit" if streamlit else "python-http"),
                           ("magic-link.js", "node-http")):
        path = root / name
        if path.is_file():
            if "SESSIONS_REVOKED" in path.read_text(encoding="utf-8", errors="replace"):
                return None
            return (tr("%s in the app is older than small 0.8: it knows neither sign-in with Google and Microsoft nor "
                       "signing out. Replace it with the new one: %s",
                       "%s в приложении старше small 0.8: не знает ни входа через Google и Microsoft, ни выхода "
                       "на всех устройствах. Замените его новым: %s") % (name, TEMPLATES_DIR / template / name))
    return None  # no magic_link next to the code: the app may keep it elsewhere, as for email sign-in


def email_login_problem(root, state=None):
    """None if the app code supports sign-in by email; otherwise, what is in the way and how to fix it.

    Streamlit cannot be wrapped in middleware: there sign-in by email is done by require_access() in app.py via
    MagicLinkGate from magic_link.py, so new versions of both files are needed. Behind the gateway the app's code
    doesn't matter: the gateway signs people in."""
    if gateway.is_on(state or {}):
        return None
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
