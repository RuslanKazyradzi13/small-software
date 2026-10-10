"""small sso: sign-in with Google or Microsoft (OpenID Connect) instead of, or next to, emailed sign-in links.

google: a Google Cloud OAuth client; work accounts of Google Workspace and Gmail addresses.
microsoft: an app registration in Microsoft Entra; members of that one organization (tenant).
Who may sign in is still the access list: small access add @company.com. small prints the redirect address to
register, asks for the client ID and secret (the secret with hidden input), checks them with the provider and writes
them to the app's variables.
"""

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .. import access as tokens
from ..common import CliError, app_dir, ask, say
from ..config import CONFIG_FILE, STATE_FILE, load_config, load_state
from ..i18n import tr
from .env import add_no_deploy, change_env, report_env_change
from .mail import secret_input

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CONSOLE = "https://console.cloud.google.com/auth/clients/create"
MICROSOFT_LOGIN_URL = "https://login.microsoftonline.com"
ENTRA_REGISTER = "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/CreateApplicationBlade"
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
GOOGLE_ID_RE = re.compile(r"[0-9]+-[0-9a-z]+\.apps\.googleusercontent\.com\Z")
TENANT_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")


def register(sub):
    s = sub.add_parser("sso", help=tr("sign-in with Google or Microsoft: google, microsoft, off",
                                      "вход через Google или Microsoft: google, microsoft, off"),
                       description=tr("Sign-in with a work account: \"Sign in with Google\" (Google Workspace and "
                                      "Gmail) or \"Sign in with Microsoft\" (members of your Microsoft Entra "
                                      "organization). Who may sign in is still the access list (small access add "
                                      "@company.com). google / microsoft: set up (the secret comes from "
                                      "GOOGLE_CLIENT_SECRET / MICROSOFT_CLIENT_SECRET or hidden input); off: turn "
                                      "off; with no subcommand, shows the status.",
                                      "Вход рабочим аккаунтом: «Войти через Google» (Google Workspace и Gmail) или "
                                      "«Войти через Microsoft» (сотрудники вашей организации в Microsoft Entra). Кто "
                                      "может войти — по-прежнему список доступа (small access add @company.com). "
                                      "google / microsoft — настроить (секрет из GOOGLE_CLIENT_SECRET / "
                                      "MICROSOFT_CLIENT_SECRET или ввод без эха); off — выключить; без подкоманды — "
                                      "статус."))
    s.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    s.set_defaults(func=cmd_sso_status)
    sso_sub = s.add_subparsers(dest="sso_command", metavar="<google|microsoft|off>")
    sg = sso_sub.add_parser("google", help=tr("set up \"Sign in with Google\"", "настроить «Войти через Google»"))
    sg.add_argument("--client-id", help=tr("OAuth client ID (…apps.googleusercontent.com)",
                                           "ID клиента OAuth (…apps.googleusercontent.com)"))
    sg.set_defaults(func=cmd_sso_google)
    sm = sso_sub.add_parser("microsoft", help=tr("set up \"Sign in with Microsoft\"",
                                                 "настроить «Войти через Microsoft»"))
    sm.add_argument("--client-id", help=tr("Application (client) ID", "Application (client) ID"))
    sm.add_argument("--tenant", help=tr("Directory (tenant) ID, or your organization's domain (company.com)",
                                        "Directory (tenant) ID или домен вашей организации (company.com)"))
    sm.set_defaults(func=cmd_sso_microsoft)
    so = sso_sub.add_parser("off", help=tr("turn sign-in with a provider off", "выключить вход через провайдера"))
    so.add_argument("provider", nargs="?", choices=tuple(tokens.SSO_VARS),
                    help=tr("which one (default: both)", "какой (по умолчанию оба)"))
    so.set_defaults(func=cmd_sso_off)
    for parser in (sg, sm, so):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))
        add_no_deploy(parser)


def prepare(root):
    """(state, redirect URI) of an app that can take single sign-on; otherwise CliError with what to do."""
    state = load_state(root / STATE_FILE)
    problem = tokens.feature_problem(root, state)
    if problem:
        raise CliError(problem)
    uri = tokens.redirect_uri(root, state)
    if uri is None:
        raise CliError(tr("the app has no address yet, and Google and Microsoft need one to send people back to. "
                          "Run small deploy first",
                          "у приложения ещё нет адреса, а Google и Microsoft должны знать, куда вернуть человека. "
                          "Сначала: small deploy"))
    return state, uri


def post_form(url, fields):
    """(HTTP status, JSON answer) of a form POST; status None if the server can't be reached."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode("ascii"), headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": "small-cli"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())
    except OSError as e:
        return None, {"error": str(getattr(e, "reason", e))}


def _json(raw):
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def check_client(provider, url, fields):
    """Show the provider a made-up code with our client ID and secret. A wrong ID or secret is refused as
    invalid_client before the code is even looked at; invalid_grant means the client itself was accepted."""
    status, answer = post_form(url, dict(fields, grant_type="authorization_code", code="small-cli-check"))
    error = str(answer.get("error") or "")
    detail = str(answer.get("error_description") or "").splitlines()[0][:200] if answer.get("error_description") else ""
    name = tokens.SSO_NAMES[provider]
    if error == "invalid_grant":
        say(tr("%s accepted the client ID and secret.", "%s принял ID клиента и секрет.") % name)
        return
    if error in ("invalid_client", "unauthorized_client") or (status == 401 and error):
        raise CliError(tr("%s did not accept the client ID or secret (%s%s). Copy both again from the client's page",
                          "%s не принял ID клиента или секрет (%s%s). Скопируйте оба ещё раз со страницы клиента")
                       % (name, error, ": " + detail if detail else ""))
    say(tr("Could not fully check the credentials with %s (%s); the first sign-in will show whether they work.",
           "Не удалось до конца проверить данные в %s (%s): первая попытка входа покажет, работают ли они.")
        % (name, error or status or tr("no answer", "нет ответа")))


def finish(root, args, state, provider, values):
    changes = dict(values)
    env, _ = tokens.app_env(root, state, warn=say)
    if not env.get("SESSION_SECRET"):
        changes["SESSION_SECRET"] = tokens.new_token()  # signs the sign-on state and the sessions
    # The real address for the redirect: RAILWAY_PUBLIC_DOMAIN goes stale after the domain is renamed.
    extra = {}
    if state.get("domain") and not load_config(root / CONFIG_FILE)["env"].get("PUBLIC_URL"):
        extra["PUBLIC_URL"] = "https://" + state["domain"]
    linked = change_env(root, changes, deploy=not args.no_deploy, railway_only=extra)
    report_env_change(linked, not args.no_deploy, tr("Sign-in with %s is on", "Вход через %s включён")
                      % tokens.SSO_NAMES[provider])
    emails = tokens.parse_emails(env.get("ACCESS_EMAILS"))
    if emails:
        say(tr("Who can sign in: %s (small access add / revoke)", "Кто может войти: %s (small access add / revoke)")
            % ", ".join(emails))
    else:
        say(tr("Nobody is on the access list yet. Add your team: small access add @company.com",
               "В списке доступа пока никого. Добавьте команду: small access add @company.com"))
    print(provider)


def cmd_sso_google(args):
    root = app_dir(args.path)
    state, uri = prepare(root)
    say(tr("\"Sign in with Google\" needs an OAuth client in Google Cloud (about 5 minutes):\n"
           "  1. Open %s (pick or create a project).\n"
           "     If Google asks to set up the consent screen first: app name and your email; Audience: Internal if\n"
           "     everyone works in your Google Workspace, External for Gmail addresses (then Publish app: sign-in\n"
           "     asks only for the email, which needs no Google review).\n"
           "  2. Application type: Web application. Authorized redirect URI:\n"
           "       %s\n"
           "  3. Create, then copy the Client ID and the Client secret.",
           "Для «Войти через Google» нужен OAuth-клиент в Google Cloud (около 5 минут):\n"
           "  1. Откройте %s (выберите или создайте проект).\n"
           "     Если Google сначала попросит настроить экран согласия: название приложения и ваша почта; Audience —\n"
           "     Internal, если все работают в вашем Google Workspace, External для адресов Gmail (затем Publish app:\n"
           "     вход запрашивает только почту, для этого проверка Google не нужна).\n"
           "  2. Application type: Web application. Authorized redirect URI:\n"
           "       %s\n"
           "  3. Create, затем скопируйте Client ID и Client secret.") % (GOOGLE_CONSOLE, uri))
    client_id = (args.client_id or os.environ.get("GOOGLE_CLIENT_ID") or ask("Client ID: ") or "").strip()
    if not GOOGLE_ID_RE.match(client_id):
        raise CliError(tr("a Google client ID looks like 1234567890-abc123.apps.googleusercontent.com: --client-id",
                          "ID клиента Google выглядит как 1234567890-abc123.apps.googleusercontent.com: --client-id"))
    secret = secret_input("GOOGLE_CLIENT_SECRET", tr("Client secret (GOCSPX-…).", "Client secret (GOCSPX-…)."))
    check_client("google", os.environ.get("GOOGLE_TOKEN_URL") or GOOGLE_TOKEN_URL,
                 {"client_id": client_id, "client_secret": secret, "redirect_uri": uri})
    finish(root, args, state, "google", {"GOOGLE_CLIENT_ID": client_id, "GOOGLE_CLIENT_SECRET": secret})
    return 0


def resolve_tenant(value):
    """A tenant GUID from a GUID or a domain the tenant has verified (via the tenant's OpenID configuration)."""
    value = (value or "").strip().lower()
    if GUID_RE.match(value):
        return value
    if not TENANT_DOMAIN_RE.match(value):
        raise CliError(tr("--tenant takes the Directory (tenant) ID (a GUID) or your organization's domain",
                          "--tenant — это Directory (tenant) ID (GUID) или домен вашей организации"))
    login = (os.environ.get("MICROSOFT_LOGIN_URL") or MICROSOFT_LOGIN_URL).rstrip("/")
    url = "%s/%s/v2.0/.well-known/openid-configuration" % (login, urllib.parse.quote(value))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "small-cli"}),
                                    timeout=20) as response:
            issuer = _json(response.read()).get("issuer", "")
    except urllib.error.HTTPError:
        issuer = ""
    except OSError as e:
        raise CliError(tr("cannot reach Microsoft: %s", "нет связи с Microsoft: %s") % getattr(e, "reason", e)) from None
    found = re.search(r"/([0-9a-f-]{36})/v2\.0$", issuer)
    if not found or not GUID_RE.match(found.group(1)):
        raise CliError(tr("Microsoft knows no organization with the domain %s. Pass the Directory (tenant) ID instead",
                          "Microsoft не знает организации с доменом %s. Укажите Directory (tenant) ID") % value)
    say(tr("Organization %s: tenant %s", "Организация %s: tenant %s") % (value, found.group(1)))
    return found.group(1)


def cmd_sso_microsoft(args):
    root = app_dir(args.path)
    state, uri = prepare(root)
    say(tr("\"Sign in with Microsoft\" needs an app registration in Microsoft Entra (about 5 minutes):\n"
           "  1. Open %s\n"
           "     (Entra ID -> App registrations -> New registration).\n"
           "  2. Supported account types: Accounts in this organizational directory only (single tenant).\n"
           "     Redirect URI: platform Web,\n"
           "       %s\n"
           "  3. Register. On the Overview page copy the Application (client) ID and the Directory (tenant) ID.\n"
           "  4. Certificates & secrets -> New client secret: copy its Value (not the Secret ID).",
           "Для «Войти через Microsoft» нужна регистрация приложения в Microsoft Entra (около 5 минут):\n"
           "  1. Откройте %s\n"
           "     (Entra ID -> App registrations -> New registration).\n"
           "  2. Supported account types: Accounts in this organizational directory only (single tenant).\n"
           "     Redirect URI: платформа Web,\n"
           "       %s\n"
           "  3. Register. На странице Overview скопируйте Application (client) ID и Directory (tenant) ID.\n"
           "  4. Certificates & secrets -> New client secret: скопируйте Value (не Secret ID).") % (ENTRA_REGISTER, uri))
    client_id = (args.client_id or os.environ.get("MICROSOFT_CLIENT_ID")
                 or ask("Application (client) ID: ") or "").strip().lower()
    if not GUID_RE.match(client_id):
        raise CliError(tr("the Application (client) ID is a GUID like 11111111-2222-3333-4444-555555555555: "
                          "--client-id", "Application (client) ID — это GUID вида "
                                         "11111111-2222-3333-4444-555555555555: --client-id"))
    tenant = resolve_tenant(args.tenant or os.environ.get("MICROSOFT_TENANT_ID")
                            or ask("Directory (tenant) ID: ") or "")
    secret = secret_input("MICROSOFT_CLIENT_SECRET", tr("Client secret Value.", "Value секрета клиента."))
    login = (os.environ.get("MICROSOFT_LOGIN_URL") or MICROSOFT_LOGIN_URL).rstrip("/")
    check_client("microsoft", "%s/%s/oauth2/v2.0/token" % (login, tenant),
                 {"client_id": client_id, "client_secret": secret, "redirect_uri": uri, "scope": "openid"})
    finish(root, args, state, "microsoft", {"MICROSOFT_CLIENT_ID": client_id, "MICROSOFT_CLIENT_SECRET": secret,
                                            "MICROSOFT_TENANT_ID": tenant})
    return 0


def cmd_sso_off(args):
    root = app_dir(args.path)
    providers = [args.provider] if args.provider else list(tokens.SSO_VARS)
    names = [name for provider in providers for name in tokens.SSO_VARS[provider]]
    linked = change_env(root, unset=names, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Sign-in with %s is off", "Вход через %s выключен")
                      % tr(" and ", " и ").join(tokens.SSO_NAMES[p] for p in providers))
    return 0


def cmd_sso_status(args):
    root = app_dir(args.path)
    state = load_state(root / STATE_FILE)
    env, _ = tokens.app_env(root, state, warn=say)
    enabled = tokens.sso_enabled(env)
    for provider in tokens.SSO_VARS:
        if provider in enabled:
            detail = (tr("tenant %s", "tenant %s") % env["MICROSOFT_TENANT_ID"] if provider == "microsoft"
                      else tr("client %s", "клиент %s") % env["GOOGLE_CLIENT_ID"])
            say(tr("%s: on, %s", "%s: включён, %s") % (tokens.SSO_NAMES[provider], detail))
        else:
            say(tr("%s: off (small sso %s)", "%s: выключен (small sso %s)") % (tokens.SSO_NAMES[provider], provider))
    uri = tokens.redirect_uri(root, state)
    if uri:
        say(tr("Redirect URI to register with the provider: %s", "Redirect URI для провайдера: %s") % uri)
    print(" ".join(enabled) or "off")
    return 0
