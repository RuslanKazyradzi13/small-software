"""small access: кто может открыть приложение. По почте (ACCESS_EMAILS) — как доступ к Google Doc, или по личной
ссылке-приглашению (ACCESS_TOKEN). add выдаёт доступ, revoke отзывает, list показывает, у кого он есть."""

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


def cmd_access_list(args):
    root = app_dir(args.path)
    env, sources = tokens.app_env(root, load_state(root / STATE_FILE), warn=say)
    entries, emails = tokens.parse(env.get("ACCESS_TOKEN")), tokens.parse_emails(env.get("ACCESS_EMAILS"))
    if not entries and not emails:
        say(tr("Nobody has access: the app is closed to everyone. "
               "Grant access: small access add ivan@company.com (by email) or small access add NAME (by link)",
               "Доступа нет ни у кого — приложение закрыто для всех. "
               "Выдать: small access add ivan@company.com (по почте) или small access add ИМЯ (по ссылке)"))
        return 0
    if emails:
        say(tr("Sign-in by email (ACCESS_EMAILS from %s):", "Вход по почте (ACCESS_EMAILS из %s):")
            % sources["ACCESS_EMAILS"])
        for email in emails:
            print("  %s" % (email + tr("  (whole domain)", "  (весь домен)") if email.startswith("@") else email))
        warn_mail(env)
    if entries:
        say(tr("Personal links (ACCESS_TOKEN from %s):", "Личные ссылки (ACCESS_TOKEN из %s):")
            % sources["ACCESS_TOKEN"])
        for name, token in entries:
            print("  %-24s %s…" % (name or tr("(shared link)", "(общая ссылка)"), token[:4]))
        say(tr("Link: small link --as NAME", "Ссылка: small link --as ИМЯ"))
    say(tr("Revoke: small access revoke EMAIL|NAME", "Отозвать: small access revoke ПОЧТА|ИМЯ"))
    return 0


def cmd_access_add(args):
    root = app_dir(args.path)
    if tokens.is_email(args.name.strip().lower()):
        return add_email(root, args.name.strip().lower(), args.no_deploy)
    if not tokens.NAME_RE.match(args.name):
        raise CliError(tr("specify an email address (ivan@company.com), a domain (@company.com) or a name: "
                          "up to 40 characters, Latin letters, digits, _ . -",
                          "укажите адрес почты (ivan@company.com), домен (@company.com) или имя — до 40 символов: "
                          "латиница, цифры, _ . -"))
    state = load_state(root / STATE_FILE)
    entries, _ = tokens.current(root, state, warn=say)
    if any(name == args.name for name, _ in entries):
        raise CliError(tr("%s already has a link: small link --as %s", "у %s уже есть ссылка: small link --as %s")
                       % (args.name, args.name))
    token = tokens.new_token()
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(entries + [(args.name, token)])},
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Access granted to %s", "Доступ для %s добавлен") % args.name)
    if state.get("domain"):
        say(tr("Personal link for %s (it works like a password, send it to this person only):",
               "Личная ссылка для %s (работает как пароль — пересылайте только ему):") % args.name)
        print(tokens.invite_link(state["domain"], token), flush=True)
    else:
        say(tr("The link will be available after small deploy: small link --as %s",
               "Ссылка появится после small deploy: small link --as %s") % args.name)
    return 0


def add_email(root, email, no_deploy):
    problem = tokens.email_login_problem(root)
    if problem:
        raise CliError(problem)
    state = load_state(root / STATE_FILE)
    env, _ = tokens.app_env(root, state, warn=say)
    emails = tokens.parse_emails(env.get("ACCESS_EMAILS"))
    if email in emails:
        raise CliError(tr("%s is already on the access list", "%s уже есть в списке доступа") % email)
    changes = {"ACCESS_EMAILS": ", ".join(emails + [email])}
    if not env.get("SESSION_SECRET"):
        changes["SESSION_SECRET"] = tokens.new_token()  # ключ подписи ссылок для входа и сессий
    # Настоящий адрес для ссылок в письмах: RAILWAY_PUBLIC_DOMAIN после переименования домена устаревает.
    # Свой PUBLIC_URL в small.json (например, свой домен) не трогаем.
    extra = {}
    if state.get("domain") and not load_config(root / CONFIG_FILE)["env"].get("PUBLIC_URL"):
        extra["PUBLIC_URL"] = "https://" + state["domain"]
    linked = change_env(root, changes, deploy=not no_deploy, railway_only=extra)
    who = tr("all %s addresses", "все адреса %s") % email if email.startswith("@") else email
    report_env_change(linked, not no_deploy, tr("Email access granted: %s", "Доступ по почте: %s") % who)
    url = ("https://%s/" % state["domain"] if state.get("domain")
           else tr("the app URL (after small deploy)", "адрес приложения (после small deploy)"))
    say(tr("How to sign in: open %s and enter a work email; an email with a sign-in link will arrive.",
           "Как войти: открыть %s и ввести рабочую почту — придёт письмо со ссылкой для входа.") % url)
    warn_mail(env)
    return 0


def warn_mail(env):
    if not any(env.get(name) for name in tokens.MAIL_VARS):
        say(tr("Warning: email is not set up, so sign-in emails are not sent. Set it up: small mail resend",
               "Внимание: почта не настроена, письма для входа не уходят. Подключить: small mail resend"))


def cmd_access_revoke(args):
    root = app_dir(args.path)
    state = load_state(root / STATE_FILE)
    email = args.name.strip().lower()
    if tokens.is_email(email):
        env, _ = tokens.app_env(root, state, warn=say)
        emails = tokens.parse_emails(env.get("ACCESS_EMAILS"))
        if email not in emails:
            raise CliError(tr("%s is not on the access list. On the list: %s", "%s нет в списке доступа. Есть: %s")
                           % (email, ", ".join(emails) or "—"))
        remaining = [e for e in emails if e != email]
        linked = change_env(root, {"ACCESS_EMAILS": ", ".join(remaining)} if remaining else None,
                            unset=() if remaining else ["ACCESS_EMAILS"], deploy=not args.no_deploy)
        report_env_change(linked, not args.no_deploy,
                          tr("Access revoked for %s: sign-in stops working right after the restart, "
                             "even in a browser that is already open",
                             "Доступ %s отозван — вход закроется сразу после перезапуска, даже в открытом браузере")
                          % email)
        return 0

    entries, _ = tokens.current(root, state, warn=say)
    remaining = [(name, token) for name, token in entries if name != args.name]
    if len(remaining) == len(entries):
        raise CliError(tr("%s has no personal link. Existing links: %s", "у %s нет личной ссылки. Есть: %s")
                       % (args.name, ", ".join(n or tr("(shared)", "(общая)") for n, _ in entries) or "—"))
    if not remaining:
        say(tr("This is the last token: once it is revoked, nobody can sign in by link.",
               "Это последний токен: после отзыва войти по ссылке не сможет никто."))
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(remaining)} if remaining else None,
                        unset=() if remaining else ["ACCESS_TOKEN"], deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy,
                      tr("Access revoked for %s: their link and cookies stop working after the restart",
                         "Доступ %s отозван — его ссылка и куки перестанут действовать после перезапуска") % args.name)
    return 0
