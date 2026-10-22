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
- Per-family Google/OpenAI provider, model and encrypted keys: every family pays for its own AI usage. Switching providers retains both configurations.
- FX conversion through Frankfurter v2; the rate, date, and source stay with the
  operation.

## Architecture

- **bot** — Telegram interaction and rich messages;
- **worker** — recurring operations, receipt cleanup, and FX refresh;
- **PostgreSQL** — application data, settings, and encrypted family AI keys.

The local compose file starts PostgreSQL. Production expects PostgreSQL supplied by
the deployment environment.

## Quick start

Requirements: Docker and Docker Compose.

```bash
cp .env.example .env
```

Set the following values in the untracked `.env` before starting:

```dotenv
BOT_TOKEN=your_telegram_bot_token
OWNER_TELEGRAM_ID=your_numeric_telegram_id
LOCAL_POSTGRES_DB=budget_bot
LOCAL_POSTGRES_USER=budget
LOCAL_POSTGRES_PASSWORD=replace_with_a_random_alphanumeric_password
```

Then run `docker compose up --build`. After `/start`, open `/family` to configure the AI provider, key and model for the
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
`DEPLOY_SSH_KNOWN_HOSTS` (the independently verified SSH host-key entry),
`DEPLOY_PATH`, `DEPLOY_NETWORK`, `TELEGRAM_BOT_TOKEN`,
`BOOTSTRAP_OWNER_TELEGRAM_ID`, and `DATABASE_URL`.

The target host needs Docker, Docker Compose, and PostgreSQL on the configured
network. Run **Deploy** manually from the Actions tab.

For development outside Compose and for production, set `DATABASE_URL` privately
to your PostgreSQL connection string. Never commit it. The worker must start after
`alembic upgrade head` completes. FX rates are reference rates, not bank settlement
rates. Review extracted receipt items before confirming a payment.

Receipt recognition extracts printed product totals and deductions separately.
Decimal arithmetic validates quantities and allocates discounts before classification.
On a mismatch, one additional image verification pass checks the extracted rows;
it cannot change the original total or invent a balancing adjustment. A receipt
that still fails validation is not recorded. This is a consistency check, not a
guarantee that every product name or category was read correctly.
Extraction and classification use only original-language names, receipt text and photos.
Russian display translations are generated in a separate request AFTER categories
are fixed; they never feed back into classification, arithmetic or grouping. Original
names and evidence remain in receipt items, with translations in a separate optional
field used only by the UI. Failed translation falls back to the original text.
The separate UI translation request can increase API usage; no more expensive
model is selected automatically. Separating translation does not eliminate OCR
errors in the original language or guarantee correct category suggestions.
Classification uses only active expense categories and maps results by product ID. Ambiguous
assignments are flagged in the draft with a category-review link. Review flags
are advisory and cleared when the user chooses a category.

### Experimental staged recognition (not enabled)

`app.receipt_pipeline` separates per-photo original-language OCR, overlap mapping,
financial interpretation, Decimal validation, targeted rereading, classification,
and display translation. Rereading is bounded to two cycles with at most six
requested rows per cycle. Arithmetic failures take priority over layout questions.
This experiment is **not connected to the bot's input handlers**: passing unit tests
does not demonstrate improved recognition quality, latency, or cost.

`python -m app.receipt_benchmark --help` describes the private-file A/B runner.
Supply a snapshot of the production recognizer, identical source files/categories,
an explicit model and its input/cached-input/output prices, and `OPENAI_API_KEY`.
It does not poll Telegram or write operations. It records each model response,
elapsed time and token usage, including incomplete usage on failed calls. Costs
with missing usage are lower bounds. PDF preparation is outside model timing.
The baseline can use native PDF text during extraction; the experimental reader
currently reads page images, with native text passed only to classification.
Review product names, missing/duplicate purchases, discounts and categories against
the originals; a matching total alone is not a quality metric. Keep manifests,
snapshots containing private data, inputs and outputs outside the repository.
Do not deploy this pipeline without a successful representative comparison.

Rotating `BOT_TOKEN` changes the encryption key: retain the old token securely
until family keys have been migrated, or have each family owner re-enter their
provider keys after rotation. Receipt files are sent to the selected Google/OpenAI provider for processing; Telegram
file identifiers are retained according to the configured retention policy.

## Security and privacy

### Receipt processing and provider settings

In `/family`, the owner selects Google or OpenAI, enters that provider's API key,
and selects a model from the provider's paginated model list. Each provider keeps
its own key and model when switching. Google requires the Gemini Developer API
(`generativelanguage.googleapis.com`); a Vision-only key is insufficient.

Receipts are processed in original language: OCR, arithmetic validation,
classification and discount allocation. Product names are not translated.
One rich progress message displays the current stage and elapsed time, including
on retries from drafts or saved operations. A failed progress edit does not
discard the recognition result. Monetary validation never invents a balancing
adjustment. Review the draft before saving, especially items marked uncertain.

- Never commit `.env`, database exports, receipts, or production logs.
- Each provider key is stored per family and encrypted at rest with family/provider-specific key derivation.
- An exported database alone does not expose family keys. An attacker with both the
  database and deployment bot token could decrypt them; protect and rotate that token.
- Receipt images may be sent to the family-selected AI provider for recognition.

See [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), and the
[MIT License](LICENSE).
