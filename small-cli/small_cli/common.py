"""Общее для всех команд: ошибки, вывод, вопросы пользователю, папка приложения и токен Railway."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_FILE, load_state
from .i18n import tr
from .railway import token_from_env


class CliError(Exception):
    pass


def say(message):
    """Прогресс и пояснения — в stderr; в stdout только результат (URL, ссылка, таблица)."""
    print(message, file=sys.stderr, flush=True)


def ask(prompt):
    """Ответ пользователя или None, если терминала нет (скрипт, CI) или ввод закрыт."""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        return input().strip()
    except EOFError:
        sys.stderr.write("\n")
        return None


def confirm(prompt):
    answer = ask(prompt)
    return answer is not None and answer.lower() in ("y", "yes", "д", "да")


def app_dir(path):
    root = Path(path).resolve()
    if not root.is_dir():
        raise CliError(tr("folder not found: %s", "папка не найдена: %s") % root)
    return root


def is_linked(state):
    return all(state.get(k) for k in ("projectId", "environmentId", "serviceId"))


def linked_state(root):
    state = load_state(root / STATE_FILE)
    if not is_linked(state):
        raise CliError(tr("%s has not been deployed yet (no %s). Run `small deploy` first",
                          "%s ещё не задеплоено (нет %s). Сначала: small deploy") % (root, STATE_FILE))
    return state


def require_token():
    token = token_from_env()
    if not token:
        raise CliError(tr("Railway token is not set. Create one in Railway → Account Settings → Tokens "
                          "and put it in the RAILWAY_API_TOKEN environment variable.",
                          "не задан токен Railway. Создайте его в Railway → Account Settings → Tokens "
                          "и положите в переменную окружения RAILWAY_API_TOKEN."))
    return token


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40].strip("-") or "app"


def human_size(size):
    units = (tr("B", "Б"), tr("KB", "КБ"), tr("MB", "МБ"))
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return ("%d %s" if unit == units[0] else "%.1f %s") % (size, unit)
        size /= 1024.0


def format_elapsed(seconds):
    return "%02d:%02d" % divmod(int(seconds), 60)


def log_time(timestamp):
    """ISO-время Railway (UTC, с наносекундами) -> локальное ЧЧ:ММ:СС."""
    try:
        moment = datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return str(timestamp)
