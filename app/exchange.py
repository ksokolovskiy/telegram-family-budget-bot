"""Official Bank of Israel exchange rates for document-currency conversion."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import aiohttp

from app.receipts import ReceiptError

BOI_RATES_URL = "https://www.boi.org.il/PublicApi/GetExchangeRates"


async def convert_amount(amount: Decimal, source: str, target: str) -> tuple[Decimal, Decimal, date]:
    source, target = source.upper(), target.upper()
    if source == target:
        return amount.quantize(Decimal("0.01")), Decimal("1"), date.today()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
        async with client.get(BOI_RATES_URL) as response:
            response.raise_for_status()
            payload = await response.json()
    rates = {str(item["key"]).upper(): Decimal(str(item["currentExchangeRate"])) / Decimal(str(item.get("unit") or 1)) for item in payload.get("exchangeRates", [])}
    rates["ILS"] = Decimal("1")
    if source not in rates or target not in rates:
        raise ReceiptError(f"Банк Израиля не публикует курс для пары {source}/{target}.")
    multiplier = rates[source] / rates[target]
    return (amount * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), multiplier, date.today()
