"""small link / small open: ссылка-приглашение задеплоенного приложения (общая или личная, --as ИМЯ)."""

import webbrowser

from .. import access
from ..common import CliError, app_dir, linked_state, say
from ..config import CONFIG_FILE
from ..i18n import tr


def register(sub):
    for name, opens, text in (("open", True, tr("open the deployed app via its invite link",
                                                "открыть задеплоенное приложение по ссылке-приглашению")),
                              ("link", False, tr("print the invite link of the deployed app",
                                                 "напечатать ссылку-приглашение задеплоенного приложения"))):
        k = sub.add_parser(name, help=text, description=text.capitalize() + tr(
            ". ACCESS_TOKEN is taken from %s or, if it's not there, from the service variables in Railway "
            "(requires RAILWAY_API_TOKEN).",
            ". ACCESS_TOKEN берётся из %s, "
            "а если его там нет — из переменных сервиса в Railway (нужен RAILWAY_API_TOKEN).")
            % CONFIG_FILE)
        k.add_argument("path", nargs="?", default=".", help=tr("app folder (default: current folder)",
                                                               "папка приложения (по умолчанию текущая)"))
        k.add_argument("--as", dest="person", metavar=tr("NAME", "ИМЯ"),
                       help=tr("personal link of a person from small access", "личная ссылка человека из small access"))
        k.set_defaults(func=cmd_link, open=opens)


def cmd_link(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if not state.get("domain"):
        raise CliError(tr("the service has no public domain; run small deploy",
                          "у сервиса нет публичного домена — выполните small deploy"))
    entries, source = access.current(root, state, warn=say)
    token = access.pick(entries, args.person)
    if args.person is not None and token is None:
        raise CliError(tr("%s has no personal link. Existing links: %s. Grant one: small access add %s",
                          "у %s нет личной ссылки. Есть: %s. Выдать: small access add %s")
                       % (args.person, ", ".join(n or tr("(shared)", "(общая)") for n, _ in entries) or "—",
                          args.person))
    link = access.invite_link(state["domain"], token)
    if token:
        say(tr("Invite link%s (ACCESS_TOKEN from %s). Anyone who gets it can sign in:",
               "Ссылка-приглашение%s (ACCESS_TOKEN из %s). Кто её получит, тот войдёт:")
            % (tr(" for ", " для ") + args.person if args.person else "", source))
    else:
        say(tr("ACCESS_TOKEN not found in %s or in Railway; URL without a token:",
               "ACCESS_TOKEN не найден ни в %s, ни в Railway — адрес без токена:") % CONFIG_FILE)
    print(link, flush=True)
    if args.open:
        webbrowser.open(link)
    return 0
