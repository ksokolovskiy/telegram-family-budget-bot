# Family Budget Bot for Telegram

Privacy-conscious Telegram bot for a shared family budget. Add an expense as text,
send a receipt, review a monthly report, and keep budgets and recurring payments in
one place. The user interface is currently Russian-first.

## Features

- Quick text input, including ISO currencies (`10usd food`).
- Image and PDF receipt recognition: item categories, discounts, multi-photo receipts,
  and a mandatory total check before saving.
- Invite-only family access, roles, notifications, budgets, recurring operations, and
  expandable rich Telegram reports.
- Per-family OpenAI key and model: every family pays for its own AI usage.
- FX conversion through Frankfurter v2; the rate, date, and source stay with the
  operation.

## Architecture

- **bot** — Telegram interaction and rich messages;
- **worker** — recurring operations, receipt cleanup, and FX refresh;
- **PostgreSQL** — application data, settings, and encrypted family OpenAI keys.

The local compose file starts PostgreSQL. Production expects PostgreSQL supplied by
the deployment environment.

## Quick start

Requirements: Docker and Docker Compose.

```bash
cp .env.example .env
docker compose up --build
```

Set the following values in the untracked `.env` before starting:

```dotenv
BOT_TOKEN=your_telegram_bot_token
OWNER_TELEGRAM_ID=your_numeric_telegram_id
LOCAL_POSTGRES_DB=budget_bot
LOCAL_POSTGRES_USER=budget
LOCAL_POSTGRES_PASSWORD=change_me
```

After `/start`, open **Settings** to configure the OpenAI key and model for the
family. `BOT_TOKEN` and `OWNER_TELEGRAM_ID` are deployment-only; other product
settings live in the bot. Local Compose builds its database connection internally.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
ruff check app tests
```

`run_dev.sh` is a local convenience script; never use it for production.

## Deployment

The manual GitHub Actions workflow deploys over SSH. Configure these repository
secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_PRIVATE_KEY`, `DEPLOY_SSH_PORT`,
`DEPLOY_PATH`, `DEPLOY_NETWORK`, `TELEGRAM_BOT_TOKEN`,
`BOOTSTRAP_OWNER_TELEGRAM_ID`, and `DATABASE_URL`.

The target host needs Docker, Docker Compose, and PostgreSQL on the configured
network. Run **Deploy** manually from the Actions tab.

## Security and privacy

- Never commit `.env`, database exports, receipts, or production logs.
- The OpenAI key is stored per family and encrypted at rest.
- An exported database alone does not expose family keys. An attacker with both the
  database and deployment bot token could decrypt them; protect and rotate that token.
- Receipt images may be sent to the family-selected AI provider for recognition.

See [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), and the
[MIT License](LICENSE).
