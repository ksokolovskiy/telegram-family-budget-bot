# Telegram Family Budget Bot

AI-powered Telegram bot for quick family budget tracking: text input, receipt images, categories, monthly budgets and plan/fact reports.

## Local run

1. Create a private `.env` (it is ignored by Git) with `BOT_TOKEN`, `OWNER_TELEGRAM_ID` and `DATABASE_URL`.
2. Start the stack:

```bash
docker compose up --build
```

The local compose file starts a PostgreSQL container and runs Alembic migrations before the bot starts.

After the first start, the configured owner opens `/settings` in the bot and
sets the OpenAI API key, model, application environment and log level. These
are persisted in PostgreSQL and take effect without a restart; the API key is
always masked in the interface and is never logged. On its next save, the API
key is encrypted at rest with Fernet; its key is derived by HKDF from the
deployment's `BOT_TOKEN`, so no additional `.env` secret is introduced.
**Rotate the bot token only through a controlled procedure:** while the old
token is still available, read/decrypt the setting and save it again after
switching to the new token; otherwise the stored OpenAI key cannot be recovered
and must be entered again by the owner. The private bootstrap configuration is
needed before the process can reach the database that stores the other settings.

## Production on ksokol2

Production deploy is handled by the manual GitHub Actions workflow `.github/workflows/deploy.yml` over SSH. The server is expected to have Docker, Docker Compose, and a PostgreSQL container named `central-postgres` attached to the Docker network used by the app.

Required GitHub secrets:

- `KSOKOL2_HOST`
- `KSOKOL2_USER`
- `KSOKOL2_SSH_KEY`
- `KSOKOL2_PORT` (optional, defaults to `22` in workflow expressions)
- `BOT_TOKEN`
- `OWNER_TELEGRAM_ID`
- `DATABASE_URL`
- `DOCKER_NETWORK` (Docker network shared with `central-postgres`, for example `central`)
- `DEPLOY_PATH` (for example `/opt/telegram-family-budget-bot`)

After the secrets are configured, run the `Deploy to ksokol2` workflow manually. It writes the private bootstrap `.env` on the server from secrets and runs:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```
