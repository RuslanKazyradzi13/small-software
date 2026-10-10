"""small: micro-apps on Railway with one command.

Entry point: argument parsing and error handling. The commands themselves live in small_cli/commands/,
shared helpers in small_cli/common.py.
"""

import argparse
import codecs
import os
import sys

from . import __version__, commands
from .common import CliError
from .config import ConfigError
from .i18n import tr
from .railway import RailwayError


def main(argv=None):
    setup_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ConfigError, RailwayError) as e:
        print(tr("Error: %s", "Ошибка: %s") % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(tr("\nInterrupted. A build that has already started will keep running in Railway.",
                 "\nПрервано. Уже запущенная сборка продолжится в Railway."), file=sys.stderr)
        return 130


def setup_streams():
    """Python writes Unicode to a terminal on its own. Output to a pipe or file
    (`small logs | Select-String <Cyrillic word>`) it encodes as ANSI (cp1251), while PowerShell reads it in the
    console encoding (866 or UTF-8): Cyrillic turns into garbage and searching for it finds nothing. So for a pipe
    we write in the encoding it will be read with."""
    encoding = pipe_encoding()
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        if encoding and not stream.isatty():
            stream.reconfigure(encoding=encoding, errors="replace")
        else:
            stream.reconfigure(errors="replace")


def pipe_encoding():
    """Encoding for output to a pipe: PYTHONIOENCODING if set (small.cmd runs `python -I`, so Python itself
    does not read it), otherwise on Windows the console encoding. None means leave it as is."""
    explicit = os.environ.get("PYTHONIOENCODING", "").split(":")[0]
    if explicit:
        try:
            return codecs.lookup(explicit).name
        except LookupError:
            return None
    if sys.platform != "win32":
        return None
    codepage = _console_codepage()  # 0: the process has no console
    return "utf-8" if codepage == 65001 else "cp%d" % codepage if codepage else None


def _console_codepage():
    import ctypes

    return ctypes.windll.kernel32.GetConsoleOutputCP()


def build_parser():
    parser = argparse.ArgumentParser(prog="small", description=tr("Micro-apps on Railway with one command.",
                                                                  "Микро-приложения на Railway одной командой."))
    parser.add_argument("--version", action="version", version="small " + __version__)
    sub = parser.add_subparsers(dest="command", metavar=tr("<command>", "<команда>"))
    sub.required = True
    for module in commands.ALL:
        module.register(sub)
    return parser
