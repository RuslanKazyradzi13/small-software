"""small dashboard: локальная страница со списком приложений (сервер — в small_cli/dashboard.py)."""

import webbrowser

from .. import dashboard as server_module
from ..common import app_dir, say
from ..railway import token_from_env


def register(sub):
    b = sub.add_parser("dashboard", help="локальная страница со списком приложений",
                       description="Найти в папке приложения (small.json / .small.json, до %d уровней вглубь) "
                                   "и показать их в браузере: имя, статус, ссылки, путь." % server_module.MAX_DEPTH)
    b.add_argument("path", nargs="?", default=".", help="где искать (по умолчанию текущая папка; например, ..)")
    b.add_argument("--port", type=int, default=server_module.DEFAULT_PORT,
                   help="порт (по умолчанию %d; если занят — любой свободный)" % server_module.DEFAULT_PORT)
    b.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    b.set_defaults(func=cmd_dashboard)


def cmd_dashboard(args):
    root = app_dir(args.path)
    token = token_from_env()
    server = server_module.start_server(root, args.port, token)
    port = server.server_address[1]
    if args.port and port != args.port:
        say("Порт %d занят, использую %d" % (args.port, port))
    found = len(server_module.scan_apps(root))
    say("small dashboard: %s, приложений найдено: %d" % (root, found))
    say("Статусы деплоя из Railway: %s" % ("включены" if token else "выключены (нет RAILWAY_API_TOKEN), офлайн-режим"))
    say("Ctrl+C — остановить")
    print(server.url, flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("Дашборд остановлен")
    finally:
        server.server_close()
    return 0
