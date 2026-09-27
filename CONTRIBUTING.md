# Contributing

Do not add secrets, receipt images, database exports, real Telegram IDs, or
production logs. Keep changes focused, test behaviour changes, and run:

```bash
pytest -q
ruff check app tests
git diff --check
```

Do not modify an already deployed Alembic migration; add a new migration instead.
Describe the user-visible change, migration impact, and verification in the pull
request. Use synthetic data in screenshots.
