"""small volume: a persistent disk (Railway volume) for the app.

Without one, the app's files live only until the next deploy or restart. With one, the app keeps files at the mount
path (RAILWAY_VOLUME_MOUNT_PATH), and magic_link keeps its sign-in state there (.magic-link.db): used sign-in links
stay used after a restart, and "Sign out on all devices" works.
"""

import argparse
import re

from .. import gateway
from ..common import CliError, app_dir, linked_state, require_token, say
from ..i18n import tr
from ..railway import Railway

DEFAULT_MOUNT = "/data"
MOUNT_RE = re.compile(r"/[A-Za-z0-9._/-]+\Z")


def register(sub):
    v = sub.add_parser("volume", help=tr("a persistent disk for files and sign-in state: add / status",
                                         "постоянный диск для файлов и состояния входа: add / статус"),
                       description=tr("A Railway volume: files under its path survive deploys and restarts, and "
                                      "magic_link keeps its sign-in state there. add creates one (default %s); with "
                                      "no subcommand, shows the status. Note: with a volume, each deploy has a few "
                                      "seconds of downtime, since two copies of the app can't share the disk.",
                                      "Том Railway: файлы по его пути переживают деплои и перезапуски, и magic_link "
                                      "хранит там состояние входа. add создаёт том (по умолчанию %s); без "
                                      "подкоманды — статус. Учтите: с томом каждый деплой — это несколько секунд "
                                      "простоя, две копии приложения не могут делить диск.") % DEFAULT_MOUNT)
    v.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    v.add_argument("--gateway", action="store_true", help=tr("the gateway's volume (small gateway on): sign-in state",
                                                             "том шлюза (small gateway on): состояние входа"))
    v.set_defaults(func=cmd_volume_status)
    volume_sub = v.add_subparsers(dest="volume_command", metavar="<add>")
    va = volume_sub.add_parser("add", help=tr("create a volume: small volume add [--mount /data]",
                                              "создать том: small volume add [--mount /data]"))
    va.add_argument("--mount", default=DEFAULT_MOUNT, help=tr("where the app sees it (default %s)",
                                                              "где приложение его видит (по умолчанию %s)")
                    % DEFAULT_MOUNT)
    va.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))
    va.add_argument("--gateway", action="store_true", default=argparse.SUPPRESS,
                    help=tr("for the gateway (small gateway on): sign-in state survives its restarts",
                            "для шлюза (small gateway on): состояние входа переживает его перезапуски"))
    va.add_argument("--no-deploy", action="store_true",
                    help=tr("don't restart the app (the volume appears on the next deploy)",
                            "не перезапускать приложение (том появится при следующем деплое)"))
    va.set_defaults(func=cmd_volume_add)


def target(args, state):
    """The app's service, or with --gateway the gateway's."""
    if not getattr(args, "gateway", False):
        return state["serviceId"]
    if not gateway.is_on(state):
        raise CliError(tr("the app has no gateway (small gateway on)", "у приложения нет шлюза (small gateway on)"))
    return state["gateway"]["serviceId"]


def cmd_volume_status(args):
    root = app_dir(args.path)
    state = linked_state(root)
    volume = Railway(require_token()).service_volume(state["environmentId"], target(args, state))
    if volume is None:
        say(tr("No volume: files and sign-in state are lost on every deploy and restart. Add one: small volume add",
               "Тома нет: файлы и состояние входа пропадают при каждом деплое и перезапуске. Добавить: small volume add"))
        print("off")
        return 0
    used = volume.get("currentSizeMB")
    size = volume.get("sizeMB")
    say(tr("Volume at %s: %s of %s MB used%s", "Том в %s: занято %s из %s МБ%s") % (
        volume["mountPath"], "%.1f" % used if isinstance(used, (int, float)) else "?", size or "?",
        "" if (volume.get("state") or "READY") == "READY" else " (%s)" % volume["state"]))
    print(volume["mountPath"])
    return 0


def cmd_volume_add(args):
    root = app_dir(args.path)
    state = linked_state(root)
    mount = args.mount.rstrip("/") or "/"
    if not MOUNT_RE.match(mount) or mount in ("/app",) or "/../" in mount + "/" or mount.startswith("/app/"):
        raise CliError(tr("--mount must be an absolute path such as /data, outside /app (the app's code lives there)",
                          "--mount — абсолютный путь вроде /data, вне /app (там лежит код приложения)"))
    api = Railway(require_token())
    service_id = target(args, state)
    existing = api.service_volume(state["environmentId"], service_id)
    if existing is not None:
        say(tr("The app already has a volume at %s (Railway gives a service one volume).",
               "У приложения уже есть том в %s (Railway даёт сервису один том).") % existing["mountPath"])
        print(existing["mountPath"])
        return 0
    api.create_volume(state["projectId"], state["environmentId"], service_id, mount)
    if not args.no_deploy:
        api.redeploy(service_id, state["environmentId"])
    say(tr("Volume created at %s%s.", "Том создан в %s%s.") % (
        mount, tr(", Railway is restarting the app", ", Railway перезапускает приложение") if not args.no_deploy
        else tr("; it appears on the next deploy", "; он появится при следующем деплое")))
    say(tr("The app can keep files there (the path is in RAILWAY_VOLUME_MOUNT_PATH); magic_link keeps sign-in state "
           "in %s/.magic-link.db. Each deploy now has a few seconds of downtime.",
           "Приложение может хранить там файлы (путь — в RAILWAY_VOLUME_MOUNT_PATH); magic_link хранит состояние "
           "входа в %s/.magic-link.db. Теперь каждый деплой — несколько секунд простоя.") % mount)
    print(mount)
    return 0
