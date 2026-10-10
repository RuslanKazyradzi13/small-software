"""small init: an app scaffold from a template — a server protected by an invite link,
small.json with a unique name and ACCESS_TOKEN, .gitignore and .railwayignore."""

import argparse
import json
import secrets
from pathlib import Path

from .. import scaffold
from ..common import CliError, confirm, say, slugify
from ..config import CONFIG_FILE
from ..i18n import tr


def register(sub):
    templates = "\n".join("  %-12s %s" % (k, scaffold.description(k)) for k in scaffold.TEMPLATES)
    i = sub.add_parser("init", help=tr("create a micro-app skeleton", "создать заготовку микро-приложения"),
                       description=tr("Create a micro-app skeleton: a server protected by an invite link, "
                                      "%s, .gitignore and .railwayignore. Existing files are not overwritten "
                                      "without confirmation.",
                                      "Создать заготовку микро-приложения: сервер с защитой по ссылке-приглашению, "
                                      "%s, .gitignore и .railwayignore. Существующие файлы не перезаписываются "
                                      "без подтверждения.") % CONFIG_FILE,
                       epilog=tr("Templates:\n", "Шаблоны:\n") + templates,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    i.add_argument("path", nargs="?", default=".",
                   help=tr("app folder (default: current folder)", "папка приложения (по умолчанию текущая)"))
    i.add_argument("-t", "--template", choices=list(scaffold.TEMPLATES), default=scaffold.DEFAULT_TEMPLATE,
                   help=tr("template (default: %s)", "шаблон (по умолчанию %s)") % scaffold.DEFAULT_TEMPLATE)
    i.add_argument("--name", help=tr("project name (default: folder name with a random suffix)",
                                     "имя проекта (по умолчанию — имя папки со случайным суффиксом)"))
    i.add_argument("--force", action="store_true",
                   help=tr("overwrite existing files without asking", "перезаписать существующие файлы без вопросов"))
    i.set_defaults(func=cmd_init)


def cmd_init(args):
    root = Path(args.path).resolve()
    if root.exists() and not root.is_dir():
        raise CliError(tr("%s is a file, not a folder", "%s — это файл, а не папка") % root)
    name = slugify(args.name) if args.name else unique_name(root.name)
    config = {"name": name, "env": {"ACCESS_TOKEN": secrets.token_urlsafe(32)}}
    files = scaffold.template_files(args.template)
    files[CONFIG_FILE] = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    # Ignore files are appended to rather than overwritten, so they don't count as conflicts.
    conflicts = [rel for rel in files if (root / rel).exists()]
    if conflicts and not args.force:
        listing = ", ".join(conflicts)
        if not confirm(tr("%s already contains: %s. Overwrite? [y/N] ",
                          "В %s уже есть: %s. Перезаписать? [y/N] ") % (root, listing)):
            raise CliError(tr("%s already contains: %s. Nothing was changed. To overwrite, add --force",
                              "в %s уже есть: %s. Ничего не изменено. Чтобы перезаписать, добавьте --force")
                           % (root, listing))

    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        (root / rel).write_bytes(content)
    merged = {rel: scaffold.merge_ignore(root / rel, lines)
              for rel, lines in scaffold.ignore_rules(args.template).items()}

    labels = {"created": tr("created", "создан"), "updated": tr("updated", "дополнен"),
              "unchanged": tr("unchanged", "без изменений")}
    say(tr("Template %s in %s:", "Шаблон %s в %s:") % (args.template, root))
    width = max(len(rel) for rel in list(files) + list(merged)) + 2
    for rel in files:
        say("  %-*s%s" % (width, rel, tr("overwritten", "перезаписан") if rel in conflicts
                          else tr("created", "создан")))
    for rel, result in merged.items():
        say("  %-*s%s" % (width, rel, labels[result]))
    say("")
    say(tr("Project name: %s. ACCESS_TOKEN generated in %s; the file is in .gitignore, do not publish it.",
           "Имя проекта: %s. В %s сгенерирован ACCESS_TOKEN; файл добавлен в .gitignore, не публикуйте его.")
        % (name, CONFIG_FILE))
    say(tr("Next:", "Дальше:"))
    if root != Path.cwd().resolve():
        say("  cd \"%s\"" % root)
    say("  small deploy")
    say(tr("  small open        # open with the invite link", "  small open        # открыть по ссылке-приглашению"))
    return 0


def unique_name(folder_name):
    """Folder name + a random suffix: an explicit name in small.json links the deploy to the project with that name,
    so it must not collide with someone else's project, nor the <name>.up.railway.app subdomain with a taken one."""
    return "%s-%s" % (slugify(folder_name)[:35].strip("-") or "app", secrets.token_hex(2))
