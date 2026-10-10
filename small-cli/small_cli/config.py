"""Файлы приложения: small.json (настройки пользователя) и .small.json (привязка к Railway).

Используются `small deploy` и `small dashboard`, поэтому проверки одинаковы для обоих.
"""

import json
import re

from .i18n import tr

CONFIG_FILE = "small.json"   # настройки пользователя: имя и переменные окружения
STATE_FILE = ".small.json"   # куда CLI запоминает ID проекта, сервиса и домен
CONFIG_KEYS = ("name", "env")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(Exception):
    pass


def load_config(path):
    """Читает small.json: {"name": "...", "env": {"KEY": "value"}}. Оба поля необязательны."""
    config = {"name": None, "env": {}}
    if not path.is_file():
        return config
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # -sig: Блокнот сохраняет с BOM
    except ValueError as e:
        raise ConfigError(tr("%s: invalid JSON (%s)", "%s: некорректный JSON (%s)") % (CONFIG_FILE, e)) from None
    if not isinstance(data, dict):
        raise ConfigError(tr("%s: expected an object like {\"name\": ..., \"env\": {...}}",
                             "%s: ожидается объект вида {\"name\": ..., \"env\": {...}}") % CONFIG_FILE)
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise ConfigError(tr("%s: unknown fields %s (allowed: %s)", "%s: неизвестные поля %s (допустимы: %s)")
                          % (CONFIG_FILE, ", ".join(unknown), ", ".join(CONFIG_KEYS)))

    name = data.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(tr("%s: name must be a non-empty string",
                                 "%s: name должен быть непустой строкой") % CONFIG_FILE)
        config["name"] = name.strip()

    env = data.get("env")
    if env is None:
        return config
    if not isinstance(env, dict):
        raise ConfigError(tr("%s: env must be an object {\"NAME\": \"value\"}",
                             "%s: env должен быть объектом {\"ИМЯ\": \"значение\"}") % CONFIG_FILE)
    for key, value in env.items():
        if not ENV_NAME_RE.match(key):
            raise ConfigError(tr("%s: invalid variable name %r (Latin letters, digits, _; must not start with a digit)",
                                 "%s: недопустимое имя переменной %r (латиница, цифры, _; не с цифры)")
                              % (CONFIG_FILE, key))
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (int, float)):
            value = str(value)
        elif not isinstance(value, str):
            raise ConfigError(tr("%s: value of %s must be a string, a number or true/false",
                                 "%s: значение %s должно быть строкой, числом или true/false") % (CONFIG_FILE, key))
        config["env"][key] = value
    return config


def load_state(path):
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(tr("%s is corrupted (%s); delete it or run with --new",
                             "%s повреждён (%s); удалите его или запустите с --new") % (path, e)) from None
    if not isinstance(state, dict):
        raise ConfigError(tr("%s is corrupted (expected an object); delete it or run with --new",
                             "%s повреждён (ожидается объект); удалите его или запустите с --new") % path)
    return state


def save_state(path, state):
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
