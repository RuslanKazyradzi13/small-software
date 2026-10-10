"""small — микро-приложения на Railway одной командой.

Точка входа: разбор аргументов и обработка ошибок. Сами команды — в small_cli/commands/,
общие помощники — в small_cli/common.py.
"""

import argparse
import sys

from . import __version__, commands
from .common import CliError
from .config import ConfigError
from .railway import RailwayError


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ConfigError, RailwayError) as e:
        print("Ошибка: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано. Уже запущенная сборка продолжится в Railway.", file=sys.stderr)
        return 130


def build_parser():
    parser = argparse.ArgumentParser(prog="small", description="Микро-приложения на Railway одной командой.")
    parser.add_argument("--version", action="version", version="small " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="<команда>")
    sub.required = True
    for module in commands.ALL:
        module.register(sub)
    return parser
