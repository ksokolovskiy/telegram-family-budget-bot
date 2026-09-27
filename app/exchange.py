"""Currency conversion through Frankfurter v2 with durable quote provenance."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.receipts import ReceiptError

FRANKFURTER_API_URL = "https://api.frankfurter.dev/v2"
FRANKFURTER_CACHE_PREFIX = "frankfurter_v2_quote:"


@dataclass(frozen=True)
class FXQuote:
    rate: Decimal
    quote_date: date
    source: str


def preferred_providers(source: str, target: str) -> list[str]:
    """Use a direct official source where it covers the pair before blending."""
    currencies = {source.upper(), target.upper()}
    if "KZT" in currencies:
        return ["nbk"]
    if "ILS" in currencies:
        return ["boi"]
    return ["ecb"]


def _quote_from_payload(payload: dict, source_label: str) -> FXQuote:
    try:
        rate = Decimal(str(payload["rate"]))
        quote_date = date.fromisoformat(str(payload["date"]))
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise ReceiptError("Frankfurter вернул некорректный курс. Попробуйте позже.") from error
    if rate <= 0:
        raise ReceiptError("Frankfurter вернул некорректный курс. Попробуйте позже.")
    return FXQuote(rate=rate, quote_date=quote_date, source=source_label)


async def fetch_frankfurter_rate(source: str, target: str) -> FXQuote:
    """Get the latest quote, preferring a single official central bank.

    A pair unsupported by that bank falls back to Frankfurter's documented
    blend of official providers. The label stored with the transaction makes
    that distinction visible and immutable.
    """
    source, target = source.upper(), target.upper()
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        for provider in preferred_providers(source, target):
            url = f"{FRANKFURTER_API_URL}/providers/{provider}/rate/{source.lower()}/{target.lower()}"
            async with client.get(url) as response:
                if response.status in {404, 422}:
                    continue
                response.raise_for_status()
                return _quote_from_payload(await response.json(), f"Frankfurter v2 / {provider.upper()}")

        url = f"{FRANKFURTER_API_URL}/rate/{source.lower()}/{target.lower()}?expand=providers"
        async with client.get(url) as response:
            response.raise_for_status()
            payload = await response.json()
    providers = payload.get("providers") if isinstance(payload, dict) else None
    count = len(providers) if isinstance(providers, list) else 0
    return _quote_from_payload(payload, f"Frankfurter v2 / blended ({count} providers)")


def _cache_key(source: str, target: str) -> str:
    return f"{FRANKFURTER_CACHE_PREFIX}{source.upper()}:{target.upper()}"


async def cache_frankfurter_quote(session: AsyncSession, source: str, target: str, quote: FXQuote) -> None:
    setting = await session.get(AppSetting, _cache_key(source, target))
    payload = json.dumps(
        {
            "rate": str(quote.rate),
            "quote_date": quote.quote_date.isoformat(),
            "source": quote.source,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        separators=(",", ":"),
    )
    if setting:
        setting.value = payload
    else:
        session.add(AppSetting(key=_cache_key(source, target), value=payload))


async def cached_frankfurter_quote(session: AsyncSession, source: str, target: str) -> FXQuote | None:
    setting = await session.get(AppSetting, _cache_key(source, target))
    if not setting:
        return None
    try:
        payload = json.loads(setting.value)
        return FXQuote(
            rate=Decimal(str(payload["rate"])),
            quote_date=date.fromisoformat(payload["quote_date"]),
            source=str(payload["source"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return None


async def refresh_frankfurter_rates(session: AsyncSession) -> date:
    """Warm common quotes; every conversion still obtains a fresh pair quote."""
    newest: date | None = None
    for source, target in (("USD", "ILS"), ("EUR", "ILS"), ("KZT", "ILS")):
        try:
            quote = await fetch_frankfurter_rate(source, target)
            await cache_frankfurter_quote(session, source, target, quote)
            newest = max(newest, quote.quote_date) if newest else quote.quote_date
        except (aiohttp.ClientError, TimeoutError, ReceiptError):
            continue
    if newest is None:
        raise ReceiptError("Не удалось обновить курсы Frankfurter. Попробуйте позже.")
    return newest


async def convert_amount(
    amount: Decimal, source: str, target: str, *, session: AsyncSession | None = None
) -> tuple[Decimal, Decimal, date, str]:
    """Convert at the latest Frankfurter quote and retain exact provenance."""
    source, target = source.upper(), target.upper()
    if source == target:
        return amount.quantize(Decimal("0.01")), Decimal("1"), date.today(), "family_currency"
    try:
        quote = await fetch_frankfurter_rate(source, target)
        if session is not None:
            await cache_frankfurter_quote(session, source, target, quote)
    except (aiohttp.ClientError, TimeoutError, ReceiptError):
        quote = await cached_frankfurter_quote(session, source, target) if session is not None else None
        if not quote:
            raise ReceiptError(
                f"Не удалось получить курс Frankfurter для пары {source}/{target}. Попробуйте позже."
            )
    converted = (amount * quote.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return converted, quote.rate, quote.quote_date, quote.source
