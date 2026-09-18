from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from app.rich_bot import create_dispatcher, setup_commands
from app.config import settings
from app.db import SessionLocal
from app.settings_store import get_runtime_settings


async def main() -> None:
    # Logging starts at INFO while DB bootstrap/migrations happen, then adopts
    # the owner-managed persisted level once the connection is available.
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    if settings.owner_telegram_id is None:
        raise RuntimeError("OWNER_TELEGRAM_ID is required")
    async with SessionLocal() as session:
        runtime = await get_runtime_settings(session)
    logging.getLogger().setLevel(runtime.log_level)
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = create_dispatcher()
    await setup_commands(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
