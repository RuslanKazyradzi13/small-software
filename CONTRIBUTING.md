# Contributing to small

Thanks for helping! small is intentionally small, so a few rules keep it that way.

## Principles

- **No dependencies.** The CLI uses only the Python standard library (3.9+). The middleware is one file per language.
- **Private by default.** Anything that could expose an app or a secret needs a test proving it does not.
- **Secrets stay out of logs and output.** Tokens, keys and sign-in links are never printed.

## Setup

```bash
git clone https://github.com/RuslanKazyradzi13/small-software
cd small-software
pip install -e ./small-cli        # or add small-cli/ to PATH on Windows
```

## Tests

```bash
cd small-cli  && python -m unittest discover -s tests
cd magic-link && python -m unittest test_magic_link && node --test
```

Tests run against a mock of the Railway API — no account needed. `pip install streamlit fastapi uvicorn httpx` also
runs the template tests on the real frameworks. CI runs everything on Windows, macOS and Linux.

## Making changes

- `magic-link/magic_link.py` and `magic-link/magic-link.js` have copies in `small-cli/small_cli/templates/`. Copy
  them after editing — a test fails if the copies differ.
- New command or option? Add a test, update `small-cli/README.md` (English), `small-cli/README.ru.md` (Russian)
  and `small-cli/CHANGELOG.md`.
- Everything users read exists in English and Russian. In the CLI, wrap text in `tr("English", "Русский")` from
  `small_cli/i18n.py`; a test runs the CLI in English and fails on any Cyrillic. Pages and emails in `magic-link`
  pick the visitor's language, log lines and Telegram messages follow `OWNER_LANG`.

## Pull requests

Keep them focused, describe what changed and how you tested it. Bug reports and ideas are welcome as
[issues](https://github.com/RuslanKazyradzi13/small-software/issues).
