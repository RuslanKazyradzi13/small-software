"""small access: who can open the app. By email (ACCESS_EMAILS), like access to a Google Doc, or by a personal
invite link (ACCESS_TOKEN). add grants access, revoke takes it away, list shows who has it, log shows who signed in,
signout ends someone's sessions on all devices.

The logic itself is in list_access / grant_access / revoke_access / access_log: the "Share" button in small dashboard
and the MCP server call them too, the commands here only print the result."""

import argparse
import json
import time
from datetime import datetime, timezone

from .. import access as tokens
from ..common import CliError, app_dir, linked_state, require_token, say
from ..config import CONFIG_FILE, STATE_FILE, load_config, load_state
from ..i18n import tr
from ..railway import Railway
from .env import add_no_deploy, change_env, report_env_change


def register(sub):
    a = sub.add_parser("access", help=tr("who can open the app: by email or by personal link",
                                         "кто может открыть приложение: по почте или по личной ссылке"),
                       description=tr("add ivan@company.com: sign-in by work email (an email with a sign-in link); "
                                      "add @company.com: the whole domain; add bob: a personal invite link. "
                                      "revoke takes access away from one person, everyone else keeps theirs. "
                                      "Emails are stored in ACCESS_EMAILS, links in ACCESS_TOKEN (in %s and in "
                                      "Railway).",
                                      "add ivan@company.com — вход по рабочей почте (письмо со ссылкой для входа); "
                                      "add @company.com — весь домен; add bob — личная ссылка-приглашение. "
                                      "revoke отзывает доступ одного человека, у остальных всё остаётся. "
                                      "Адреса хранятся в ACCESS_EMAILS, ссылки — в ACCESS_TOKEN (в %s и в Railway).")
                       % CONFIG_FILE)
    a.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    a.set_defaults(func=cmd_access_list)
    access_sub = a.add_subparsers(dest="access_command", metavar="<list|add|revoke|log|signout>")
    al = access_sub.add_parser("list", help=tr("who has access", "у кого есть доступ"))
    al.set_defaults(func=cmd_access_list)
    aa = access_sub.add_parser("add", help=tr("grant access: small access add ivan@company.com | @company.com | bob",
                                              "выдать доступ: small access add ivan@company.com | @company.com | bob"))
    aa.add_argument("name", metavar=tr("EMAIL|NAME", "ПОЧТА|ИМЯ"))
    aa.set_defaults(func=cmd_access_add)
    ar = access_sub.add_parser("revoke", help=tr("revoke access: small access revoke ivan@company.com | bob",
                                                 "отозвать доступ: small access revoke ivan@company.com | bob"))
    ar.add_argument("name", metavar=tr("EMAIL|NAME", "ПОЧТА|ИМЯ"))
    ar.set_defaults(func=cmd_access_revoke)
    ag = access_sub.add_parser("log", help=tr("who signed in, when and how (from the Railway logs)",
                                              "кто, когда и как входил (из логов Railway)"))
    ag.add_argument("-n", "--lines", type=int, default=50, help=tr("how many entries (default 50)",
                                                                   "сколько записей (по умолчанию 50)"))
    ag.add_argument("--json", action="store_true", help=tr("one JSON object per line", "по объекту JSON в строке"))
    ag.set_defaults(func=cmd_access_log)
    ao = access_sub.add_parser("signout", help=tr("end sessions on all devices: small access signout "
                                                  "ivan@company.com | @company.com | --all",
                                                  "завершить входы на всех устройствах: small access signout "
                                                  "ivan@company.com | @company.com | --all"))
    ao.add_argument("name", nargs="?", metavar=tr("EMAIL", "ПОЧТА"))
    ao.add_argument("--all", action="store_true", help=tr("everyone who signed in by email or with Google or "
                                                          "Microsoft", "всех, кто вошёл по почте или через Google "
                                                                       "и Microsoft"))
    ao.set_defaults(func=cmd_access_signout)
    for parser in (al, aa, ar, ag, ao):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))
    for parser in (aa, ar, ao):
        add_no_deploy(parser)


def list_access(root, warn=None):
    """Who can open the app.

    {"emails": addresses and @domains, "entries": [(name, token)], "sources": where each variable comes from,
     "sso": single sign-on providers that are on, "sends_mail": whether sign-in emails go out, "mail": whether people
     on the list can sign in at all (emails, Google or Microsoft)}. The terminal needs the tokens (their first
    characters identify a link); the dashboard sends only the names to the browser.
    """
    env, sources = tokens.app_env(root, load_state(root / STATE_FILE), warn=warn)
    return {"emails": tokens.parse_emails(env.get("ACCESS_EMAILS")), "entries": tokens.parse(env.get("ACCESS_TOKEN")),
            "sources": sources, "sso": tokens.sso_enabled(env), "sends_mail": sends_mail(env),
            "mail": mail_ready(env)}


def cmd_access_list(args):
    root = app_dir(args.path)
    info = list_access(root, warn=say)
    entries, emails = info["entries"], info["emails"]
    if not entries and not emails:
        say(tr("Nobody has access: the app is closed to everyone. "
               "Grant access: small access add ivan@company.com (by email) or small access add NAME (by link)",
               "Доступа нет ни у кого — приложение закрыто для всех. "
               "Выдать: small access add ivan@company.com (по почте) или small access add ИМЯ (по ссылке)"))
        return 0
    if emails:
        say(tr("Sign-in by email (ACCESS_EMAILS from %s):", "Вход по почте (ACCESS_EMAILS из %s):")
            % info["sources"]["ACCESS_EMAILS"])
        for email in emails:
            print("  %s" % (email + tr("  (whole domain)", "  (весь домен)") if email.startswith("@") else email))
        ways = [tokens.SSO_NAMES[p] for p in info["sso"]] + ([tr("email link", "ссылка на почту")]
                                                            if info["sends_mail"] else [])
        if ways:
            say(tr("They sign in with: %s", "Как входят: %s") % ", ".join(ways))
        if not info["mail"]:
            say(mail_warning())
    if entries:
        say(tr("Personal links (ACCESS_TOKEN from %s):", "Личные ссылки (ACCESS_TOKEN из %s):")
            % info["sources"]["ACCESS_TOKEN"])
        for name, token in entries:
            print("  %-24s %s…" % (name or tr("(shared link)", "(общая ссылка)"), token[:4]))
        say(tr("Link: small link --as NAME", "Ссылка: small link --as ИМЯ"))
    say(tr("Revoke: small access revoke EMAIL|NAME", "Отозвать: small access revoke ПОЧТА|ИМЯ"))
    return 0


def who_error():
    """Neither an email, nor a domain, nor a name: the same hint in the terminal and in the dashboard."""
    return CliError(tr("specify an email address (ivan@company.com), a domain (@company.com) or a name: "
                       "up to 40 characters, Latin letters, digits, _ . -",
                       "укажите адрес почты (ivan@company.com), домен (@company.com) или имя — до 40 символов: "
                       "латиница, цифры, _ . -"))


def grant_access(root, who, deploy=True, warn=None):
    """Grants access to an email, a domain (@company.com) or via a personal link (a name): changes small.json and
    the variables in Railway, with deploy restarts the app. Notes along the way go to warn, the result is a dict:

    {"kind": "email" | "domain" | "link", "who", "linked": whether Railway was touched, "domain": the domain from
     .small.json, "token": the new link's token ("link" only), "mail": whether sign-in emails go out (email and
     domain only)}
    """
    email = who.strip().lower()
    if tokens.is_email(email):
        return _grant_email(root, email, deploy, warn)
    if not tokens.NAME_RE.match(who):
        raise who_error()
    state = load_state(root / STATE_FILE)
    entries, _ = tokens.current(root, state, warn=warn)
    if any(name == who for name, _ in entries):
        raise CliError(tr("%s already has a link: small link --as %s", "у %s уже есть ссылка: small link --as %s")
                       % (who, who))
    token = tokens.new_token()
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(entries + [(who, token)])}, deploy=deploy)
    return {"kind": "link", "who": who, "linked": linked, "domain": state.get("domain"), "token": token}


def _grant_email(root, email, deploy, warn):
    problem = tokens.email_login_problem(root)
    if problem:
        raise CliError(problem)
    state = load_state(root / STATE_FILE)
    env, _ = tokens.app_env(root, state, warn=warn)
    emails = tokens.parse_emails(env.get("ACCESS_EMAILS"))
    if email in emails:
        raise CliError(tr("%s is already on the access list", "%s уже есть в списке доступа") % email)
    changes = {"ACCESS_EMAILS": ", ".join(emails + [email])}
    if not env.get("SESSION_SECRET"):
        changes["SESSION_SECRET"] = tokens.new_token()  # signing key for sign-in links and sessions
    # The real address for links in emails: RAILWAY_PUBLIC_DOMAIN goes stale after the domain is renamed.
    # A custom PUBLIC_URL in small.json (e.g. your own domain) is left alone.
    extra = {}
    if state.get("domain") and not load_config(root / CONFIG_FILE)["env"].get("PUBLIC_URL"):
        extra["PUBLIC_URL"] = "https://" + state["domain"]
    linked = change_env(root, changes, deploy=deploy, railway_only=extra)
    return {"kind": "domain" if email.startswith("@") else "email", "who": email, "linked": linked,
            "domain": state.get("domain"), "mail": mail_ready(env), "sso": tokens.sso_enabled(env)}


def cmd_access_add(args):
    root = app_dir(args.path)
    deploy = not args.no_deploy
    result = grant_access(root, args.name, deploy=deploy, warn=say)
    if result["kind"] == "link":
        report_env_change(result["linked"], deploy, tr("Access granted to %s", "Доступ для %s добавлен") % args.name)
        if result["domain"]:
            say(tr("Personal link for %s (it works like a password, send it to this person only):",
                   "Личная ссылка для %s (работает как пароль — пересылайте только ему):") % args.name)
            print(tokens.invite_link(result["domain"], result["token"]), flush=True)
        else:
            say(tr("The link will be available after small deploy: small link --as %s",
                   "Ссылка появится после small deploy: small link --as %s") % args.name)
        return 0
    email = result["who"]
    who = tr("all %s addresses", "все адреса %s") % email if result["kind"] == "domain" else email
    report_env_change(result["linked"], deploy, tr("Email access granted: %s", "Доступ по почте: %s") % who)
    url = ("https://%s/" % result["domain"] if result["domain"]
           else tr("the app URL (after small deploy)", "адрес приложения (после small deploy)"))
    if result["sso"]:
        say(tr("How to sign in: open %s and sign in with %s.", "Как войти: открыть %s и войти через %s.")
            % (url, tr(" or ", " или ").join(tokens.SSO_NAMES[p] for p in result["sso"])))
    else:
        say(tr("How to sign in: open %s and enter a work email; an email with a sign-in link will arrive.",
               "Как войти: открыть %s и ввести рабочую почту — придёт письмо со ссылкой для входа.") % url)
    if not result["mail"]:
        say(mail_warning())
    return 0


def sends_mail(env):
    """Whether sign-in emails go out: Resend, SMTP or the test mode (links go to the app log) is set up."""
    return any(env.get(name) for name in tokens.MAIL_VARS)


def mail_ready(env):
    """Whether people on the list can sign in: sign-in emails go out, or sign-in with Google or Microsoft is on."""
    return sends_mail(env) or bool(tokens.sso_enabled(env))


def mail_warning():
    return tr("Warning: nobody on the list can sign in yet: neither sign-in emails nor Google or Microsoft are set up. "
              "Set up one: small sso google, small sso microsoft or small mail resend",
              "Внимание: войти из списка пока никто не может — не настроены ни письма для входа, ни Google или "
              "Microsoft. Настройте что-то одно: small sso google, small sso microsoft или small mail resend")


def revoke_access(root, who, deploy=True, warn=None):
    """Revokes access for an email, a domain or a personal link (who="" is the shared link without a name); the rest
    keep theirs. Changes small.json and Railway, with deploy restarts the app. Result: {"kind", "who", "linked"}."""
    state = load_state(root / STATE_FILE)
    email = who.strip().lower()
    if tokens.is_email(email):
        env, _ = tokens.app_env(root, state, warn=warn)
        emails = tokens.parse_emails(env.get("ACCESS_EMAILS"))
        if email not in emails:
            raise CliError(tr("%s is not on the access list. On the list: %s", "%s нет в списке доступа. Есть: %s")
                           % (email, ", ".join(emails) or "—"))
        remaining = [e for e in emails if e != email]
        # Their sessions end for good: should the address be added back later, the old sessions stay invalid.
        changes = {"SESSIONS_REVOKED": tokens.add_revoked(env.get("SESSIONS_REVOKED"), email, time.time())}
        if remaining:
            changes["ACCESS_EMAILS"] = ", ".join(remaining)
        linked = change_env(root, changes, unset=() if remaining else ["ACCESS_EMAILS"], deploy=deploy)
        return {"kind": "domain" if email.startswith("@") else "email", "who": email, "linked": linked}

    entries, _ = tokens.current(root, state, warn=warn)
    remaining = [(name, token) for name, token in entries if name != who]
    if len(remaining) == len(entries):
        raise CliError(tr("%s has no personal link. Existing links: %s", "у %s нет личной ссылки. Есть: %s")
                       % (who, ", ".join(n or tr("(shared)", "(общая)") for n, _ in entries) or "—"))
    if not remaining and warn:
        warn(tr("This is the last token: once it is revoked, nobody can sign in by link.",
                "Это последний токен: после отзыва войти по ссылке не сможет никто."))
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(remaining)} if remaining else None,
                        unset=() if remaining else ["ACCESS_TOKEN"], deploy=deploy)
    return {"kind": "link", "who": who, "linked": linked}


def cmd_access_revoke(args):
    root = app_dir(args.path)
    deploy = not args.no_deploy
    result = revoke_access(root, args.name, deploy=deploy, warn=say)
    if result["kind"] == "link":
        report_env_change(result["linked"], deploy,
                          tr("Access revoked for %s: their link and cookies stop working after the restart",
                             "Доступ %s отозван — его ссылка и куки перестанут действовать после перезапуска")
                          % args.name)
    else:
        report_env_change(result["linked"], deploy,
                          tr("Access revoked for %s: sign-in stops working right after the restart, "
                             "even in a browser that is already open",
                             "Доступ %s отозван — вход закроется сразу после перезапуска, даже в открытом браузере")
                          % result["who"])
    return 0


def signout_access(root, who, deploy=True, warn=None):
    """End the sessions of an address, a domain (@company.com) or everyone ("*") on all devices: SESSIONS_REVOKED
    gets the current time, and sessions issued before it stop working. Access itself stays: they can sign in again.
    Invite-link cookies are not affected (revoke the link instead). Result: {"who", "linked"}."""
    who = who.strip().lower()
    if who != "*" and not tokens.is_email(who):
        raise CliError(tr("specify an email address (ivan@company.com), a domain (@company.com) or --all",
                          "укажите адрес почты (ivan@company.com), домен (@company.com) или --all"))
    problem = tokens.feature_problem(root)
    if problem:
        raise CliError(problem)
    env, _ = tokens.app_env(root, load_state(root / STATE_FILE), warn=warn)
    value = tokens.add_revoked(env.get("SESSIONS_REVOKED"), who, time.time())
    return {"who": who, "linked": change_env(root, {"SESSIONS_REVOKED": value}, deploy=deploy)}


def cmd_access_signout(args):
    root = app_dir(args.path)
    if bool(args.name) == bool(args.all):
        raise CliError(tr("specify an email address (ivan@company.com), a domain (@company.com) or --all",
                          "укажите адрес почты (ivan@company.com), домен (@company.com) или --all"))
    deploy = not args.no_deploy
    result = signout_access(root, "*" if args.all else args.name, deploy=deploy, warn=say)
    who = tr("everyone", "все") if result["who"] == "*" else result["who"]
    report_env_change(result["linked"], deploy,
                      tr("Signed out on all devices: %s. Sessions from before now stop working after the restart; "
                         "access stays, so they can sign in again",
                         "Выход на всех устройствах: %s. Входы до этого момента перестанут действовать после "
                         "перезапуска; доступ остаётся, войти можно заново") % who)
    if result["who"] != "*":
        say(tr("To take access away as well: small access revoke %s",
               "Чтобы ещё и забрать доступ: small access revoke %s") % result["who"])
    return 0


# What each event of magic_link's JSON log is called in the table.
EVENTS = {"sign_in": ("signed in", "вход"), "denied": ("refused", "отказ"),
          "sign_out_all": ("signed out everywhere", "выход везде"), "request": ("access request", "запрос доступа")}
METHODS = {"email": ("email link", "ссылка на почту"), "link": ("invite link", "личная ссылка"),
           "google": ("Google", "Google"), "microsoft": ("Microsoft", "Microsoft")}


def access_log(root, limit=50):
    """Sign-ins, refusals, sign-outs and access requests, oldest first: [{"time", "event", "method", "user", "ip",
    "ua"}]. magic_link (0.8+) writes them as JSON log lines; Railway keeps them across deploys, for as long as the
    plan keeps logs."""
    state = linked_state(root)
    entries = Railway(require_token()).environment_logs(
        state["environmentId"], "@service:%s @magic_link:*" % state["serviceId"], limit=limit)
    rows = []
    for entry in entries:
        fields = entry["attributes"]
        if fields.get("magic_link") not in EVENTS:
            continue
        rows.append({"time": entry["timestamp"], "event": fields["magic_link"], "method": fields.get("method") or "",
                     "user": fields.get("user") or "", "ip": fields.get("ip") or "", "ua": fields.get("ua") or ""})
    return rows[-limit:]


def cmd_access_log(args):
    if not 1 <= args.lines <= 1000:
        raise CliError(tr("--lines must be from 1 to 1000", "--lines — от 1 до 1000"))
    root = app_dir(args.path)
    rows = access_log(root, args.lines)
    if args.json:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if not rows:
        say(tr("No sign-ins in the log yet. Railway keeps logs for a limited time, depending on the plan; apps with "
               "magic_link older than small 0.8 don't write this log.",
               "В логе пока нет входов. Railway хранит логи ограниченное время, в зависимости от тарифа; приложения "
               "с magic_link старше small 0.8 этот журнал не пишут."))
        return 0
    for row in rows:
        event = tr(*EVENTS[row["event"]])
        method = tr(*METHODS[row["method"]]) if row["method"] in METHODS else row["method"]
        who = row["user"] or (tr("(shared link)", "(общая ссылка)") if row["method"] == "link" else "—")
        print("%s  %-16s %-15s %-32s %s" % (_local_time(row["time"]), event, method, who, row["ip"] or "—"))
    return 0


def _local_time(timestamp):
    """Railway ISO time (UTC, with nanoseconds) -> local "YYYY-MM-DD HH:MM"."""
    try:
        moment = datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(timestamp)
