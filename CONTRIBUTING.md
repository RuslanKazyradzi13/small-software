# Contributing to small

Thanks for helping! small is intentionally small, so a few rules keep it that way.

## Principles

- **No dependencies.** The CLI uses only the Python standard library (3.8+). The middleware is one file per language.
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
- New command or option? Add a test, update `small-cli/README.md` and `small-cli/CHANGELOG.md`.
- User-facing pages and emails exist in English and Russian; add both strings.

## Pull requests

Keep them focused, describe what changed and how you tested it. Bug reports and ideas are welcome as
[issues](https://github.com/RuslanKazyradzi13/small-software/issues).
