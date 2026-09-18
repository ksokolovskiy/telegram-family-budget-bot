"""Dedicated worker for scheduled finance maintenance."""
from __future__ import annotations

import asyncio
import logging

from app.db import SessionLocal
from app.repositories import (
    list_due_recurring_transactions,
    list_receipts_due_for_purge,
    materialize_recurring_transaction,
    purge_transaction_receipt,
)


async def run_maintenance() -> None:
    """Materialize due rules and purge expired receipt media every five minutes."""
    while True:
        try:
            async with SessionLocal() as session, session.begin():
                for recurring in await list_due_recurring_transactions(session):
                    await materialize_recurring_transaction(session, recurring)
                for transaction in await list_receipts_due_for_purge(session):
                    await purge_transaction_receipt(session, transaction)
        except Exception:
            logging.getLogger(__name__).exception("Scheduled budget maintenance failed")
        await asyncio.sleep(300)


async def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await run_maintenance()


if __name__ == "__main__":
    asyncio.run(main())
