"""Language of small's messages: English or Russian.

SMALL_LANG=en|ru sets the language explicitly. Otherwise it follows the locale: LC_ALL, LC_MESSAGES, LANG,
LANGUAGE, and on Windows, where these are usually absent, the system UI language. Anything that is not Russian
is English.

In code: tr("Done", "<Russian text>"). The language is chosen at call time, so translate inside functions, not in
module-level constants.
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
        if value and value != "POSIX" and not value.startswith("C"):  # C, C.UTF-8: no locale chosen
            return "ru" if value.lower().startswith("ru") else "en"
    if platform == "win32":
        return "ru" if _windows_ui_language() & 0x3FF == 0x19 else "en"  # 0x19 is LANG_RUSSIAN
    return "en"


def _windows_ui_language():
    try:
        import ctypes

        return ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:  # not Windows or no kernel32, so not Russian
        return 0


def lang():
    global _lang
    if _lang is None:
        _lang = detect()
    return _lang


def use(language):
    """Set the language explicitly (tests). None means detect it from the environment again."""
    global _lang
    _lang = language if language in LANGS else None


def tr(en, ru):
    return ru if lang() == "ru" else en
