# Telegram Family Budget Bot

AI-powered Telegram bot for quick family budget tracking: text input, receipt images, categories, monthly budgets and plan/fact reports.

## Local run

1. Copy `.env.example` to `.env` and set `BOT_TOKEN` and `OPENAI_API_KEY`.
2. Start the stack:

```bash
docker compose up --build
```

The local compose file starts a PostgreSQL container and runs Alembic migrations before the bot starts.

## Production on ksokol2

Production deploy is handled by the manual GitHub Actions workflow `.github/workflows/deploy.yml` over SSH. The server is expected to have Docker, Docker Compose, and a PostgreSQL container named `central-postgres` attached to the Docker network used by the app.

Required GitHub secrets:

- `KSOKOL2_HOST`
- `KSOKOL2_USER`
- `KSOKOL2_SSH_KEY`
- `KSOKOL2_PORT` (optional, defaults to `22` in workflow expressions)
- `BOT_TOKEN`
- `OPENAI_API_KEY`
- `OPENAI_MODEL` (optional, defaults to `gpt-4o-mini`)
- `POSTGRES_CONTAINER` (defaults conceptually to `central-postgres`)
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `DOCKER_NETWORK` (Docker network shared with `central-postgres`, for example `central`)
- `DEPLOY_PATH` (for example `/opt/telegram-family-budget-bot`)

After the secrets are configured, run the `Deploy to ksokol2` workflow manually. The workflow creates the database if it does not exist, writes `.env` on the server from secrets, and runs:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```
