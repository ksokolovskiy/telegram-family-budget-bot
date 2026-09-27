"""Dedicated worker for scheduled finance maintenance."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.exchange import refresh_boi_rates
from app.repositories import (
    list_due_recurring_transactions,
    list_receipts_due_for_purge,
    materialize_recurring_transaction,
    purge_transaction_receipt,
)


async def run_maintenance() -> None:
    """Run finance maintenance and refresh official FX quotes every six hours."""
    last_fx_refresh: datetime | None = None
    while True:
        try:
            async with SessionLocal() as session, session.begin():
                for recurring in await list_due_recurring_transactions(session):
                    await materialize_recurring_transaction(session, recurring)
                for transaction in await list_receipts_due_for_purge(session):
                    await purge_transaction_receipt(session, transaction)
                now = datetime.now(timezone.utc)
                if last_fx_refresh is None or now - last_fx_refresh >= timedelta(hours=6):
                    _, rate_date = await refresh_boi_rates(session)
                    logging.getLogger(__name__).info("Refreshed Bank of Israel FX rates for %s", rate_date)
                    last_fx_refresh = now
        except Exception:
            logging.getLogger(__name__).exception("Scheduled budget maintenance failed")
        await asyncio.sleep(300)


async def main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await run_maintenance()


if __name__ == "__main__":
    asyncio.run(main())
