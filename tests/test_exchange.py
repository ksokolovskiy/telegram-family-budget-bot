from datetime import date
from decimal import Decimal

import pytest

from app.exchange import FXQuote, _quote_from_payload, convert_amount, preferred_providers
from app.receipts import ReceiptError


def test_prefers_direct_official_provider_for_ils_and_kzt():
    assert preferred_providers("USD", "ILS") == ["boi"]
    assert preferred_providers("KZT", "ILS") == ["nbk"]
    assert preferred_providers("USD", "EUR") == ["ecb"]


def test_parses_frankfurter_v2_quote_and_rejects_invalid_rate():
    quote = _quote_from_payload({"date": "2026-09-25", "rate": 3.033}, "Frankfurter v2 / BOI")

    assert quote == FXQuote(Decimal("3.033"), date(2026, 9, 25), "Frankfurter v2 / BOI")
    with pytest.raises(ReceiptError):
        _quote_from_payload({"date": "2026-09-25", "rate": 0}, "Frankfurter v2 / BOI")


async def test_conversion_persists_the_quote_provider(monkeypatch):
    async def fake_quote(source, target):
        assert (source, target) == ("USD", "ILS")
        return FXQuote(Decimal("3.033"), date(2026, 9, 25), "Frankfurter v2 / BOI")

    monkeypatch.setattr("app.exchange.fetch_frankfurter_rate", fake_quote)

    amount, rate, rate_date, source = await convert_amount(Decimal("10"), "usd", "ils")

    assert amount == Decimal("30.33")
    assert rate == Decimal("3.033")
    assert rate_date == date(2026, 9, 25)
    assert source == "Frankfurter v2 / BOI"
