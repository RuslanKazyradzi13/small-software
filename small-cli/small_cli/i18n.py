"""Язык сообщений small: английский или русский.

SMALL_LANG=en|ru задаёт язык явно. Иначе — по локали: LC_ALL, LC_MESSAGES, LANG, LANGUAGE, а на Windows, где их
обычно нет, — по языку интерфейса системы. Всё, что не русский, — английский.

В коде: tr("Done", "Готово"). Язык выбирается при вызове, поэтому переводить надо внутри функций, а не в
константах модуля.
"""

import os
import sys

LANGS = ("en", "ru")
_lang = None


def detect(environ=None, platform=None):
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    explicit = environ.get("SMALL_LANG", "").strip().lower()[:2]
    if explicit in LANGS:
        return explicit
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = environ.get(name, "").strip()
        if value and value != "POSIX" and not value.startswith("C"):  # C, C.UTF-8 — локаль не выбрана
            return "ru" if value.lower().startswith("ru") else "en"
    if platform == "win32":
        return "ru" if _windows_ui_language() & 0x3FF == 0x19 else "en"  # 0x19 — LANG_RUSSIAN
    return "en"


def _windows_ui_language():
    try:
        import ctypes

        return ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:  # не Windows или нет kernel32 — значит, не русский
        return 0


def lang():
    global _lang
    if _lang is None:
        _lang = detect()
    return _lang


def use(language):
    """Задать язык явно (тесты). None — снова определить по окружению."""
    global _lang
    _lang = language if language in LANGS else None


def tr(en, ru):
    return ru if lang() == "ru" else en
