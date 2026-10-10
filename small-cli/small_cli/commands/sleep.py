"""small sleep: режим serverless Railway — без запросов приложение засыпает и не тратит ресурсы,
первый запрос его будит (несколько секунд задержки)."""

import argparse

from ..common import app_dir, linked_state, require_token, say
from ..railway import Railway


def register(sub):
    s = sub.add_parser("sleep", help="засыпать без запросов (экономия): on / off / статус",
                       description="Режим сна Railway (serverless): без входящих запросов приложение засыпает и "
                                   "не расходует CPU и память; первый запрос будит его за несколько секунд. "
                                   "Удобно для приложений, которые открывают пару раз в день.")
    s.add_argument("mode", nargs="?", choices=("on", "off"), help="включить или выключить; без аргумента — статус")
    s.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    s.add_argument("--no-deploy", action="store_true",
                   help="не перезапускать приложение (настройка вступит в силу при следующем деплое)")
    s.set_defaults(func=cmd_sleep)


def cmd_sleep(args):
    root = app_dir(args.path)
    state = linked_state(root)
    api = Railway(require_token())
    sid, eid = state["serviceId"], state["environmentId"]
    current = api.instance_status(sid, eid)
    if args.mode is None:
        deployment = current["deployment"]
        say("Режим сна: %s%s" % (
            "включён — без запросов приложение засыпает" if current["sleep"] else "выключен — приложение работает всегда",
            "; сейчас: %s" % deployment["status"] if deployment else ""))
        print("on" if current["sleep"] else "off")
        return 0
    wanted = args.mode == "on"
    if current["sleep"] == wanted:
        say("Режим сна уже %s." % ("включён" if wanted else "выключен"))
        return 0
    api.update_instance(sid, eid, sleepApplication=wanted)
    if not args.no_deploy:
        api.redeploy(sid, eid)
    say("Режим сна %s%s." % ("включён" if wanted else "выключен",
                             ", Railway перезапускает приложение" if not args.no_deploy else
                             "; вступит в силу при следующем деплое"))
    if wanted:
        say("Первый запрос после простоя будет на несколько секунд дольше: приложение просыпается.")
    return 0
