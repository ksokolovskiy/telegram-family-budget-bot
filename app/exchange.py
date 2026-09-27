"""Official Bank of Israel exchange rates and a durable refresh fallback."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree import ElementTree

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.receipts import ReceiptError

BOI_RATES_URL = "https://www.boi.org.il/PublicApi/GetExchangeRates"
NBK_RATES_URL = "https://nationalbank.kz/rss/get_rates.cfm?fdate={date}"
BOI_CACHE_KEY = "boi_exchange_rates_v1"


def exchange_rate_source(source: str, target: str) -> str:
    """Human-readable provenance stored with an immutable transaction."""
    return "Bank of Israel + NB Kazakhstan" if "KZT" in {source.upper(), target.upper()} else "Bank of Israel"


async def fetch_boi_rates() -> tuple[dict[str, Decimal], date]:
    """Fetch the published daily table; never invent a rate or a rate date."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
        async with client.get(BOI_RATES_URL) as response:
            response.raise_for_status()
            payload = await response.json()
    rates = {
        str(item["key"]).upper(): Decimal(str(item["currentExchangeRate"])) / Decimal(str(item.get("unit") or 1))
        for item in payload.get("exchangeRates", [])
    }
    if not rates:
        raise ReceiptError("Банк Израиля вернул пустой список курсов. Попробуйте позже.")
    update_dates = [
        datetime.fromisoformat(str(item["lastUpdate"]).replace("Z", "+00:00")).date()
        for item in payload.get("exchangeRates", [])
        if item.get("lastUpdate")
    ]
    rates["ILS"] = Decimal("1")
    return rates, max(update_dates, default=date.today())


async def fetch_kzt_ils_rate() -> tuple[Decimal, date]:
    """Get the official KZT/ILS cross-rate published by Kazakhstan's NBK.

    The Bank of Israel does not publish KZT. NBK does publish ILS/KZT and
    calculates its non-USD pairs through its own official cross-rate process.
    A weekend can have no new quote, so use the latest published business day.
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
        for days_back in range(8):
            quote_date = date.today() - timedelta(days=days_back)
            url = NBK_RATES_URL.format(date=quote_date.strftime("%d.%m.%Y"))
            try:
                async with client.get(url) as response:
                    response.raise_for_status()
                    content = await response.read()
                root = ElementTree.fromstring(content)
                published = datetime.strptime(root.findtext("date", ""), "%d.%m.%Y").date()
                for item in root.findall("item"):
                    if (item.findtext("title") or "").strip().upper() != "ILS":
                        continue
                    kzt_per_unit = Decimal((item.findtext("description") or "").strip().replace(",", "."))
                    units = Decimal((item.findtext("quant") or "1").strip())
                    if kzt_per_unit <= 0 or units <= 0:
                        break
                    return units / kzt_per_unit, published
            except (aiohttp.ClientError, TimeoutError, ElementTree.ParseError, ValueError, InvalidOperation, ArithmeticError):
                continue
    raise ReceiptError("Не удалось обновить официальный курс KZT Национального банка Казахстана.")


async def refresh_boi_rates(session: AsyncSession) -> tuple[dict[str, Decimal], date]:
    """Persist the official table so temporary BOI outages don't stop drafts."""
    rates, rate_date = await fetch_boi_rates()
    try:
        rates["KZT"], kzt_date = await fetch_kzt_ils_rate()
        # A KZT conversion uses both official sources; report the older quote
        # date instead of pretending that both were refreshed later.
        rate_date = min(rate_date, kzt_date)
    except ReceiptError:
        cached = await cached_boi_rates(session)
        if cached and "KZT" in cached[0]:
            rates["KZT"] = cached[0]["KZT"]
    setting = await session.get(AppSetting, BOI_CACHE_KEY)
    payload = json.dumps(
        {
            "rates": {code: str(rate) for code, rate in rates.items()},
            "rate_date": rate_date.isoformat(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        separators=(",", ":"),
    )
    if setting:
        setting.value = payload
    else:
        session.add(AppSetting(key=BOI_CACHE_KEY, value=payload))
    return rates, rate_date


async def cached_boi_rates(session: AsyncSession) -> tuple[dict[str, Decimal], date] | None:
    setting = await session.get(AppSetting, BOI_CACHE_KEY)
    if not setting:
        return None
    try:
        payload = json.loads(setting.value)
        rates = {str(code).upper(): Decimal(str(value)) for code, value in payload["rates"].items()}
        return rates, date.fromisoformat(payload["rate_date"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def convert_amount(
    amount: Decimal, source: str, target: str, *, session: AsyncSession | None = None
) -> tuple[Decimal, Decimal, date]:
    source, target = source.upper(), target.upper()
    if source == target:
        return amount.quantize(Decimal("0.01")), Decimal("1"), date.today()
    try:
        if session is None:
            rates, rate_date = await fetch_boi_rates()
            if "KZT" in {source, target}:
                rates["KZT"], kzt_date = await fetch_kzt_ils_rate()
                rate_date = min(rate_date, kzt_date)
        else:
            rates, rate_date = await refresh_boi_rates(session)
    except (aiohttp.ClientError, TimeoutError, ReceiptError):
        cached = await cached_boi_rates(session) if session is not None else None
        if not cached:
            raise ReceiptError("Не удалось обновить курсы Банка Израиля. Попробуйте позже.")
        rates, rate_date = cached
    if source not in rates or target not in rates:
        raise ReceiptError(
            f"Банк Израиля не публикует курс для пары {source}/{target}. "
            "Эту валюту можно хранить как валюту семьи, но автоматически конвертировать её сейчас нельзя."
        )
    multiplier = rates[source] / rates[target]
    return (amount * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), multiplier, rate_date
