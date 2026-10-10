"""small access: кто может открыть приложение. По почте (ACCESS_EMAILS) — как доступ к Google Doc, или по личной
ссылке-приглашению (ACCESS_TOKEN). add выдаёт доступ, revoke отзывает, list показывает, у кого он есть."""

import argparse

from .. import access as tokens
from ..common import CliError, app_dir, say
from ..config import CONFIG_FILE, STATE_FILE, load_config, load_state
from .env import add_no_deploy, change_env, report_env_change


def register(sub):
    a = sub.add_parser("access", help="кто может открыть приложение: по почте или по личной ссылке",
                       description="add ivan@company.com — вход по рабочей почте (письмо со ссылкой для входа); "
                                   "add @company.com — весь домен; add bob — личная ссылка-приглашение. "
                                   "revoke отзывает доступ одного человека, у остальных всё остаётся. "
                                   "Адреса хранятся в ACCESS_EMAILS, ссылки — в ACCESS_TOKEN (в %s и в Railway)."
                                   % CONFIG_FILE)
    a.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    a.set_defaults(func=cmd_access_list)
    access_sub = a.add_subparsers(dest="access_command", metavar="<list|add|revoke>")
    al = access_sub.add_parser("list", help="у кого есть доступ")
    al.set_defaults(func=cmd_access_list)
    aa = access_sub.add_parser("add", help="выдать доступ: small access add ivan@company.com | @company.com | bob")
    aa.add_argument("name", metavar="ПОЧТА|ИМЯ")
    aa.set_defaults(func=cmd_access_add)
    ar = access_sub.add_parser("revoke", help="отозвать доступ: small access revoke ivan@company.com | bob")
    ar.add_argument("name", metavar="ПОЧТА|ИМЯ")
    ar.set_defaults(func=cmd_access_revoke)
    for parser in (al, aa, ar):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")
    for parser in (aa, ar):
        add_no_deploy(parser)


def cmd_access_list(args):
    root = app_dir(args.path)
    env, sources = tokens.app_env(root, load_state(root / STATE_FILE), warn=say)
    entries, emails = tokens.parse(env.get("ACCESS_TOKEN")), tokens.parse_emails(env.get("ACCESS_EMAILS"))
    if not entries and not emails:
        say("Доступа нет ни у кого — приложение закрыто для всех. "
            "Выдать: small access add ivan@company.com (по почте) или small access add ИМЯ (по ссылке)")
        return 0
    if emails:
        say("Вход по почте (ACCESS_EMAILS из %s):" % sources["ACCESS_EMAILS"])
        for email in emails:
            print("  %s" % (email + "  (весь домен)" if email.startswith("@") else email))
        warn_mail(env)
    if entries:
        say("Личные ссылки (ACCESS_TOKEN из %s):" % sources["ACCESS_TOKEN"])
        for name, token in entries:
            print("  %-24s %s…" % (name or "(общая ссылка)", token[:4]))
        say("Ссылка: small link --as ИМЯ")
    say("Отозвать: small access revoke ПОЧТА|ИМЯ")
    return 0


def cmd_access_add(args):
    root = app_dir(args.path)
    if tokens.is_email(args.name.strip().lower()):
        return add_email(root, args.name.strip().lower(), args.no_deploy)
    if not tokens.NAME_RE.match(args.name):
        raise CliError("укажите адрес почты (ivan@company.com), домен (@company.com) или имя — до 40 символов: "
                       "латиница, цифры, _ . -")
    state = load_state(root / STATE_FILE)
    entries, _ = tokens.current(root, state, warn=say)
    if any(name == args.name for name, _ in entries):
        raise CliError("у %s уже есть ссылка: small link --as %s" % (args.name, args.name))
    token = tokens.new_token()
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(entries + [(args.name, token)])},
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Доступ для %s добавлен" % args.name)
    if state.get("domain"):
        say("Личная ссылка для %s (работает как пароль — пересылайте только ему):" % args.name)
        print(tokens.invite_link(state["domain"], token), flush=True)
    else:
        say("Ссылка появится после small deploy: small link --as %s" % args.name)
    return 0


def add_email(root, email, no_deploy):
    problem = tokens.email_login_problem(root)
    if problem:
        raise CliError(problem)
    state = load_state(root / STATE_FILE)
    env, _ = tokens.app_env(root, state, warn=say)
    emails = tokens.parse_emails(env.get("ACCESS_EMAILS"))
    if email in emails:
        raise CliError("%s уже есть в списке доступа" % email)
    changes = {"ACCESS_EMAILS": ", ".join(emails + [email])}
    if not env.get("SESSION_SECRET"):
        changes["SESSION_SECRET"] = tokens.new_token()  # ключ подписи ссылок для входа и сессий
    # Настоящий адрес для ссылок в письмах: RAILWAY_PUBLIC_DOMAIN после переименования домена устаревает.
    # Свой PUBLIC_URL в small.json (например, свой домен) не трогаем.
    extra = {}
    if state.get("domain") and not load_config(root / CONFIG_FILE)["env"].get("PUBLIC_URL"):
        extra["PUBLIC_URL"] = "https://" + state["domain"]
    linked = change_env(root, changes, deploy=not no_deploy, railway_only=extra)
    who = "все адреса %s" % email if email.startswith("@") else email
    report_env_change(linked, not no_deploy, "Доступ по почте: %s" % who)
    url = "https://%s/" % state["domain"] if state.get("domain") else "адрес приложения (после small deploy)"
    say("Как войти: открыть %s и ввести рабочую почту — придёт письмо со ссылкой для входа." % url)
    warn_mail(env)
    return 0


def warn_mail(env):
    if not any(env.get(name) for name in tokens.MAIL_VARS):
        say("Внимание: почта не настроена, письма для входа не уходят. Подключить: small mail resend")


def cmd_access_revoke(args):
    root = app_dir(args.path)
    state = load_state(root / STATE_FILE)
    email = args.name.strip().lower()
    if tokens.is_email(email):
        env, _ = tokens.app_env(root, state, warn=say)
        emails = tokens.parse_emails(env.get("ACCESS_EMAILS"))
        if email not in emails:
            raise CliError("%s нет в списке доступа. Есть: %s" % (email, ", ".join(emails) or "—"))
        remaining = [e for e in emails if e != email]
        linked = change_env(root, {"ACCESS_EMAILS": ", ".join(remaining)} if remaining else None,
                            unset=() if remaining else ["ACCESS_EMAILS"], deploy=not args.no_deploy)
        report_env_change(linked, not args.no_deploy,
                          "Доступ %s отозван — вход закроется сразу после перезапуска, даже в открытом браузере"
                          % email)
        return 0

    entries, _ = tokens.current(root, state, warn=say)
    remaining = [(name, token) for name, token in entries if name != args.name]
    if len(remaining) == len(entries):
        raise CliError("у %s нет личной ссылки. Есть: %s"
                       % (args.name, ", ".join(n or "(общая)" for n, _ in entries) or "—"))
    if not remaining:
        say("Это последний токен: после отзыва войти по ссылке не сможет никто.")
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(remaining)} if remaining else None,
                        unset=() if remaining else ["ACCESS_TOKEN"], deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy,
                      "Доступ %s отозван — его ссылка и куки перестанут действовать после перезапуска" % args.name)
    return 0
