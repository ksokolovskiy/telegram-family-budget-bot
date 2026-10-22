"""One editable progress message, with elapsed time during long provider calls."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress

from app.rich import esc


@asynccontextmanager
async def receipt_progress(bot, chat_id, rich):
    message_id = None
    started = time.monotonic()
    current = "Загружаю и подготавливаю чек…"
    lock = asyncio.Lock()

    async def show(text=None):
        nonlocal current, message_id
        async with lock:
            if text:
                current = text
            html = (
                f"<p>{esc(current)} <small>Прошло {int(time.monotonic() - started)} с</small></p>"
            )
            try:
                if message_id is None:
                    message_id = (await rich.send(bot, chat_id, html)).get("message_id")
                else:
                    await rich.edit(bot, chat_id, message_id, html)
            except Exception:
                # A Telegram progress update must never discard a paid OCR result.
                logging.getLogger(__name__).warning("Could not update receipt progress")

    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            await show()

    await show()
    task = asyncio.create_task(heartbeat())
    failed = False
    try:
        yield show
    except BaseException:
        failed = True
        raise
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await show("Обработка остановлена. Чек не записан." if failed else "Обработка завершена.")
