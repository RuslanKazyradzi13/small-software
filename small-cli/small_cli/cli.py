"""small — микро-приложения на Railway одной командой.

Точка входа: разбор аргументов и обработка ошибок. Сами команды — в small_cli/commands/,
общие помощники — в small_cli/common.py.
"""

import argparse
import codecs
import os
import sys

from . import __version__, commands
from .common import CliError
from .config import ConfigError
from .railway import RailwayError


def main(argv=None):
    setup_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ConfigError, RailwayError) as e:
        print("Ошибка: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано. Уже запущенная сборка продолжится в Railway.", file=sys.stderr)
        return 130


def setup_streams():
    """В терминал Python пишет Unicode сам. Вывод в канал или файл (`small logs | Select-String вход`) он кодирует
    в ANSI (cp1251), а PowerShell читает его в кодировке консоли (866 или UTF-8): кириллица превращается в мусор,
    и поиск по ней ничего не находит. Поэтому в канал пишем в кодировке, которой его прочитают."""
    encoding = pipe_encoding()
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        if encoding and not stream.isatty():
            stream.reconfigure(encoding=encoding, errors="replace")
        else:
            stream.reconfigure(errors="replace")


def pipe_encoding():
    """Кодировка для вывода в канал: PYTHONIOENCODING, если задана (small.cmd запускает `python -I`, и сам Python
    её не читает), иначе на Windows — кодировка консоли. None — оставить как есть."""
    explicit = os.environ.get("PYTHONIOENCODING", "").split(":")[0]
    if explicit:
        try:
            return codecs.lookup(explicit).name
        except LookupError:
            return None
    if sys.platform != "win32":
        return None
    codepage = _console_codepage()  # 0 — у процесса нет консоли
    return "utf-8" if codepage == 65001 else "cp%d" % codepage if codepage else None


def _console_codepage():
    import ctypes

    return ctypes.windll.kernel32.GetConsoleOutputCP()


def build_parser():
    parser = argparse.ArgumentParser(prog="small", description="Микро-приложения на Railway одной командой.")
    parser.add_argument("--version", action="version", version="small " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="<команда>")
    sub.required = True
    for module in commands.ALL:
        module.register(sub)
    return parser
