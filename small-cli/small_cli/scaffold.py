"""Шаблоны для `small init`: файлы приложения и правила .gitignore / .railwayignore."""

from pathlib import Path

from .i18n import tr

TEMPLATES_DIR = Path(__file__).parent / "templates"
DEFAULT_TEMPLATE = "python-http"

# description — (английский, русский): язык выбирается при вызове, см. description().
TEMPLATES = {
    "python-http": {
        "description": ("Python 3, standard library (wsgiref) + magic_link.py",
                        "Python 3, стандартная библиотека (wsgiref) + magic_link.py"),
        "gitignore": ["__pycache__/", "*.pyc", ".venv/", "venv/"],
    },
    "node-http": {
        "description": ("Node.js 18+, http module + magic-link.js",
                        "Node.js 18+, модуль http + magic-link.js"),
        "gitignore": ["node_modules/", "npm-debug.log*"],
    },
    "fastapi": {
        "description": ("FastAPI + uvicorn (ASGI), protected by MagicLinkASGI from magic_link.py",
                        "FastAPI + uvicorn (ASGI), защита MagicLinkASGI из magic_link.py"),
        "gitignore": ["__pycache__/", "*.pyc", ".venv/", "venv/"],
    },
    "streamlit": {
        "description": ("Streamlit: interactive Python UI, protected by MagicLinkGate from magic_link.py",
                        "Streamlit: интерактивный интерфейс на Python, защита MagicLinkGate из magic_link.py"),
        "gitignore": ["__pycache__/", "*.pyc", ".venv/", "venv/", ".streamlit/secrets.toml"],
    },
}

# small.json хранит ACCESS_TOKEN и другие секреты, .small.json — ID проекта на этой машине.
GITIGNORE_COMMON = ["small.json", ".small.json", ".env"]
# Нужно в git, но не нужно в Railway.
RAILWAYIGNORE = ["*.md", "tests/", ".vscode/", ".idea/"]

IGNORE_HEADERS = {  # (английский, русский)
    ".gitignore": ("# Do not commit", "# Не коммитить"),
    ".railwayignore": ("# Do not upload to Railway (in addition to .gitignore)",
                       "# Не загружать в Railway (в дополнение к .gitignore)"),
}


def description(template):
    """Описание шаблона на языке пользователя (для `small init --help`)."""
    return tr(*TEMPLATES[template]["description"])


def template_files(template):
    """{имя файла: содержимое} файлов приложения из шаблона."""
    folder = TEMPLATES_DIR / template
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}


def ignore_rules(template):
    return {
        ".gitignore": GITIGNORE_COMMON + TEMPLATES[template]["gitignore"],
        ".railwayignore": RAILWAYIGNORE,
    }


def merge_ignore(path, lines):
    """Создаёт ignore-файл или дописывает в него недостающие строки, ничего не удаляя.

    Возвращает "created", "updated" или "unchanged".
    """
    exists = path.is_file()
    text = path.read_text(encoding="utf-8-sig") if exists else ""
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in lines if line not in present]
    if not missing:
        return "unchanged"
    if text and not text.endswith("\n"):
        text += "\n"
    header = tr("\n# Added by small init\n", "\n# Добавлено small init\n") if exists \
        else tr(*IGNORE_HEADERS[path.name]) + "\n"
    text += header + "\n".join(missing) + "\n"
    path.write_bytes(text.encode("utf-8"))
    return "updated" if exists else "created"
