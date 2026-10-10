"""small init: заготовка приложения из шаблона — сервер с защитой по ссылке-приглашению,
small.json с уникальным именем и ACCESS_TOKEN, .gitignore и .railwayignore."""

import argparse
import json
import secrets
from pathlib import Path

from .. import scaffold
from ..common import CliError, confirm, say, slugify
from ..config import CONFIG_FILE


def register(sub):
    templates = "\n".join("  %-12s %s" % (k, v["description"]) for k, v in scaffold.TEMPLATES.items())
    i = sub.add_parser("init", help="создать заготовку микро-приложения",
                       description="Создать заготовку микро-приложения: сервер с защитой по ссылке-приглашению, "
                                   "%s, .gitignore и .railwayignore. Существующие файлы не перезаписываются "
                                   "без подтверждения." % CONFIG_FILE,
                       epilog="Шаблоны:\n" + templates, formatter_class=argparse.RawDescriptionHelpFormatter)
    i.add_argument("path", nargs="?", default=".", help="папка приложения (по умолчанию текущая)")
    i.add_argument("-t", "--template", choices=list(scaffold.TEMPLATES), default=scaffold.DEFAULT_TEMPLATE,
                   help="шаблон (по умолчанию %s)" % scaffold.DEFAULT_TEMPLATE)
    i.add_argument("--name", help="имя проекта (по умолчанию — имя папки со случайным суффиксом)")
    i.add_argument("--force", action="store_true", help="перезаписать существующие файлы без вопросов")
    i.set_defaults(func=cmd_init)


def cmd_init(args):
    root = Path(args.path).resolve()
    if root.exists() and not root.is_dir():
        raise CliError("%s — это файл, а не папка" % root)
    name = slugify(args.name) if args.name else unique_name(root.name)
    config = {"name": name, "env": {"ACCESS_TOKEN": secrets.token_urlsafe(32)}}
    files = scaffold.template_files(args.template)
    files[CONFIG_FILE] = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    # Ignore-файлы не перезаписываются, а дополняются, поэтому конфликтом не считаются.
    conflicts = [rel for rel in files if (root / rel).exists()]
    if conflicts and not args.force:
        listing = ", ".join(conflicts)
        if not confirm("В %s уже есть: %s. Перезаписать? [y/N] " % (root, listing)):
            raise CliError("в %s уже есть: %s. Ничего не изменено. Чтобы перезаписать, добавьте --force"
                           % (root, listing))

    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        (root / rel).write_bytes(content)
    merged = {rel: scaffold.merge_ignore(root / rel, lines)
              for rel, lines in scaffold.ignore_rules(args.template).items()}

    labels = {"created": "создан", "updated": "дополнен", "unchanged": "без изменений"}
    say("Шаблон %s в %s:" % (args.template, root))
    width = max(len(rel) for rel in list(files) + list(merged)) + 2
    for rel in files:
        say("  %-*s%s" % (width, rel, "перезаписан" if rel in conflicts else "создан"))
    for rel, result in merged.items():
        say("  %-*s%s" % (width, rel, labels[result]))
    say("")
    say("Имя проекта: %s. В %s сгенерирован ACCESS_TOKEN; файл добавлен в .gitignore, не публикуйте его."
        % (name, CONFIG_FILE))
    say("Дальше:")
    if root != Path.cwd().resolve():
        say("  cd \"%s\"" % root)
    say("  small deploy")
    say("  small open        # открыть по ссылке-приглашению")
    return 0


def unique_name(folder_name):
    """Имя папки + случайный суффикс: явное имя из small.json привязывает деплой к одноимённому проекту,
    поэтому оно не должно совпасть с чужим проектом, а поддомен <name>.up.railway.app — с занятым."""
    return "%s-%s" % (slugify(folder_name)[:35].strip("-") or "app", secrets.token_hex(2))
