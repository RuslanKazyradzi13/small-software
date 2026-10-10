# Small Software

Микро-приложения на [Railway](https://railway.com) одной командой: создать заготовку, задеплоить, раздать доступ
по ссылке-приглашению, посмотреть логи и удалить.

```
small init my-app        # сервер с защитой по ссылке, small.json, .gitignore
cd my-app
small deploy             # → https://my-app-3f9a.up.railway.app
small open               # открыть по ссылке-приглашению
small logs -f            # логи
small dashboard          # все приложения в браузере
small destroy            # удалить из Railway
```

| Папка | Что внутри |
|---|---|
| [small-cli](small-cli/README.md) | утилита `small` (Python 3.8+, без зависимостей): `init`, `deploy`, `link`/`open`, `logs`, `destroy`, `dashboard` |
| [magic-link](magic-link/README.md) | доступ по ссылке-приглашению: middleware для Python (WSGI) и Node.js; его же используют шаблоны `small init` |
| [manual-test-app](manual-test-app) | приложение, созданное `small init`, — пример того, что получается из шаблона `python-http` |

## Установка

Из корня склонированного репозитория:

```bash
pip install ./small-cli
```

На Windows `pip install` создаёт `small.exe`, который Smart App Control может заблокировать. Тогда вместо установки
добавьте папку `small-cli` в `PATH`: команду `small` будет обслуживать `small.cmd`. Подробности и настройка
токена Railway — в [small-cli/README.md](small-cli/README.md).

## Разработка

```bash
cd small-cli && python -m unittest discover -s tests        # 74 теста; тесты Streamlit — если он установлен
cd magic-link && python -m unittest test_magic_link && node --test
```

Те же тесты на каждый push гоняет GitHub Actions на Windows, Linux и macOS: [.github/workflows/tests.yml](.github/workflows/tests.yml).
История изменений — [small-cli/CHANGELOG.md](small-cli/CHANGELOG.md).

Секреты в репозиторий не попадают: `small.json` (в нём `ACCESS_TOKEN`) и `.small.json` (ID проектов) в `.gitignore`.
