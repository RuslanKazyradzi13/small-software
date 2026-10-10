"""small access: who can open the app. By email (ACCESS_EMAILS), like access to a Google Doc, or by a personal
invite link (ACCESS_TOKEN). add grants access, revoke takes it away, list shows who has it.

The logic itself is in list_access / grant_access / revoke_access: the "Share" button in small dashboard calls them
too, the commands here only print the result."""

import argparse

from .. import access as tokens
from ..common import CliError, app_dir, say
from ..config import CONFIG_FILE, STATE_FILE, load_config, load_state
from ..i18n import tr
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
    access_sub = a.add_subparsers(dest="access_command", metavar="<list|add|revoke>")
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
    for parser in (al, aa, ar):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))
    for parser in (aa, ar):
        add_no_deploy(parser)


def list_access(root, warn=None):
    """Who can open the app.

    {"emails": addresses and @domains, "entries": [(name, token)], "sources": where each variable comes from,
     "mail": whether sign-in emails go out}. The terminal needs the tokens (their first characters identify a link);
    the dashboard sends only the names to the browser.
    """
    env, sources = tokens.app_env(root, load_state(root / STATE_FILE), warn=warn)
    return {"emails": tokens.parse_emails(env.get("ACCESS_EMAILS")), "entries": tokens.parse(env.get("ACCESS_TOKEN")),
            "sources": sources, "mail": mail_ready(env)}


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
            "domain": state.get("domain"), "mail": mail_ready(env)}


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
    say(tr("How to sign in: open %s and enter a work email; an email with a sign-in link will arrive.",
           "Как войти: открыть %s и ввести рабочую почту — придёт письмо со ссылкой для входа.") % url)
    if not result["mail"]:
        say(mail_warning())
    return 0


def mail_ready(env):
    """Whether sign-in emails go out: Resend, SMTP or the test mode (links go to the app log) is set up."""
    return any(env.get(name) for name in tokens.MAIL_VARS)


def mail_warning():
    return tr("Warning: email is not set up, so sign-in emails are not sent. Set it up: small mail resend",
              "Внимание: почта не настроена, письма для входа не уходят. Подключить: small mail resend")


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
        linked = change_env(root, {"ACCESS_EMAILS": ", ".join(remaining)} if remaining else None,
                            unset=() if remaining else ["ACCESS_EMAILS"], deploy=deploy)
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
