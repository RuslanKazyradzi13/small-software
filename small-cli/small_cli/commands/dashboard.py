"""small dashboard: локальная страница со списком приложений (сервер — в small_cli/dashboard.py)."""

import webbrowser

from .. import dashboard as server_module
from ..common import app_dir, say
from ..i18n import tr
from ..railway import token_from_env


def register(sub):
    b = sub.add_parser("dashboard", help=tr("local page that lists your apps",
                                            "локальная страница со списком приложений"),
                       description=tr("Find apps in a folder (small.json / .small.json, up to %d levels deep) "
                                      "and show them in the browser: name, status, links, path.",
                                      "Найти в папке приложения (small.json / .small.json, до %d уровней вглубь) "
                                      "и показать их в браузере: имя, статус, ссылки, путь.") % server_module.MAX_DEPTH)
    b.add_argument("path", nargs="?", default=".", help=tr("where to look (default: current folder; e.g. ..)",
                                                           "где искать (по умолчанию текущая папка; например, ..)"))
    b.add_argument("--port", type=int, default=server_module.DEFAULT_PORT,
                   help=tr("port (default %d; if it's busy, any free one)",
                           "порт (по умолчанию %d; если занят — любой свободный)") % server_module.DEFAULT_PORT)
    b.add_argument("--no-browser", action="store_true", help=tr("don't open the browser", "не открывать браузер"))
    b.set_defaults(func=cmd_dashboard)


def cmd_dashboard(args):
    root = app_dir(args.path)
    token = token_from_env()
    server = server_module.start_server(root, args.port, token)
    port = server.server_address[1]
    if args.port and port != args.port:
        say(tr("Port %d is busy, using %d", "Порт %d занят, использую %d") % (args.port, port))
    found = len(server_module.scan_apps(root))
    say(tr("small dashboard: %s, apps found: %d", "small dashboard: %s, приложений найдено: %d") % (root, found))
    say(tr("Deploy statuses from Railway: %s", "Статусы деплоя из Railway: %s")
        % (tr("on", "включены") if token else tr("off (no RAILWAY_API_TOKEN), offline mode",
                                                 "выключены (нет RAILWAY_API_TOKEN), офлайн-режим")))
    say(tr("Ctrl+C to stop", "Ctrl+C — остановить"))
    print(server.url, flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say(tr("Dashboard stopped", "Дашборд остановлен"))
    finally:
        server.server_close()
    return 0
