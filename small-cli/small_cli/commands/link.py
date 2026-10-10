"""small link / small open: ссылка-приглашение задеплоенного приложения (общая или личная, --as ИМЯ)."""

import webbrowser

from .. import access
from ..common import CliError, app_dir, linked_state, say
from ..config import CONFIG_FILE


def register(sub):
    for name, opens, text in (("open", True, "открыть задеплоенное приложение по ссылке-приглашению"),
                              ("link", False, "напечатать ссылку-приглашение задеплоенного приложения")):
        k = sub.add_parser(name, help=text, description=text.capitalize() + ". ACCESS_TOKEN берётся из %s, "
                           "а если его там нет — из переменных сервиса в Railway (нужен RAILWAY_API_TOKEN)."
                           % CONFIG_FILE)
        k.add_argument("path", nargs="?", default=".", help="папка приложения (по умолчанию текущая)")
        k.add_argument("--as", dest="person", metavar="ИМЯ", help="личная ссылка человека из small access")
        k.set_defaults(func=cmd_link, open=opens)


def cmd_link(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if not state.get("domain"):
        raise CliError("у сервиса нет публичного домена — выполните small deploy")
    entries, source = access.current(root, state, warn=say)
    token = access.pick(entries, args.person)
    if args.person is not None and token is None:
        raise CliError("у %s нет личной ссылки. Есть: %s. Выдать: small access add %s"
                       % (args.person, ", ".join(n or "(общая)" for n, _ in entries) or "—", args.person))
    link = access.invite_link(state["domain"], token)
    if token:
        say("Ссылка-приглашение%s (ACCESS_TOKEN из %s). Кто её получит, тот войдёт:"
            % (" для " + args.person if args.person else "", source))
    else:
        say("ACCESS_TOKEN не найден ни в %s, ни в Railway — адрес без токена:" % CONFIG_FILE)
    print(link, flush=True)
    if args.open:
        webbrowser.open(link)
    return 0
