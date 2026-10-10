"""small access: токен на каждого человека — выдать личную ссылку, отозвать, посмотреть, у кого есть доступ."""

import argparse

from .. import access as tokens
from ..common import CliError, app_dir, say
from ..config import CONFIG_FILE, STATE_FILE, load_state
from .env import add_no_deploy, change_env, report_env_change


def register(sub):
    a = sub.add_parser("access", help="личные ссылки-приглашения: список, выдать, отозвать",
                       description="Токен на каждого человека. add выдаёт личную ссылку, revoke отзывает её: "
                                   "ссылка и куки этого человека перестают работать, у остальных всё остаётся. "
                                   "Токены хранятся в ACCESS_TOKEN (в %s и в Railway)." % CONFIG_FILE)
    a.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    a.set_defaults(func=cmd_access_list)
    access_sub = a.add_subparsers(dest="access_command", metavar="<list|add|revoke>")
    al = access_sub.add_parser("list", help="у кого есть доступ")
    al.set_defaults(func=cmd_access_list)
    aa = access_sub.add_parser("add", help="выдать личную ссылку: small access add bob")
    aa.add_argument("name", metavar="ИМЯ")
    aa.set_defaults(func=cmd_access_add)
    ar = access_sub.add_parser("revoke", help="отозвать доступ: small access revoke bob")
    ar.add_argument("name", metavar="ИМЯ")
    ar.set_defaults(func=cmd_access_revoke)
    for parser in (al, aa, ar):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")
    for parser in (aa, ar):
        add_no_deploy(parser)


def cmd_access_list(args):
    root = app_dir(args.path)
    entries, source = tokens.current(root, load_state(root / STATE_FILE), warn=say)
    if not entries:
        say("ACCESS_TOKEN не задан — защищённое приложение закрыто для всех. Выдать доступ: small access add ИМЯ")
        return 0
    say("Доступ (ACCESS_TOKEN из %s):" % source)
    for name, token in entries:
        print("  %-24s %s…" % (name or "(общая ссылка)", token[:4]))
    say("Личная ссылка: small link --as ИМЯ · отозвать: small access revoke ИМЯ")
    return 0


def cmd_access_add(args):
    root = app_dir(args.path)
    if not tokens.NAME_RE.match(args.name):
        raise CliError("имя — до 40 символов: латиница, цифры, _ . @ -")
    state = load_state(root / STATE_FILE)
    entries, _ = tokens.current(root, state, warn=say)
    if any(name == args.name for name, _ in entries):
        raise CliError("у %s уже есть ссылка: small link --as %s" % (args.name, args.name))
    token = tokens.new_token()
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(entries + [(args.name, token)])},
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Доступ для %s добавлен" % args.name)
    if state.get("domain"):
        say("Личная ссылка для %s:" % args.name)
        print(tokens.invite_link(state["domain"], token), flush=True)
    else:
        say("Ссылка появится после small deploy: small link --as %s" % args.name)
    return 0


def cmd_access_revoke(args):
    root = app_dir(args.path)
    entries, _ = tokens.current(root, load_state(root / STATE_FILE), warn=say)
    remaining = [(name, token) for name, token in entries if name != args.name]
    if len(remaining) == len(entries):
        raise CliError("у %s нет личной ссылки. Есть: %s"
                       % (args.name, ", ".join(n or "(общая)" for n, _ in entries) or "—"))
    if not remaining:
        say("Это последний токен: после отзыва приложение будет закрыто для всех.")
    linked = change_env(root, {"ACCESS_TOKEN": tokens.serialize(remaining)} if remaining else None,
                        unset=() if remaining else ["ACCESS_TOKEN"], deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy,
                      "Доступ %s отозван — его ссылка и куки перестанут действовать после перезапуска" % args.name)
    return 0
