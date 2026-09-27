from decimal import Decimal

from app.ai import (
    AIParser,
    ParsedTransaction,
    ReceiptItem,
    SYSTEM_PROMPT,
    fallback_parse_text,
    reconcile_receipt_totals,
    receipt_totals_match,
)
from app.rich_bot import draft_html, inline_payment_parts, normalize_text_comment


def test_system_prompt_replaces_categories_without_interpreting_json_braces():
    prompt = SYSTEM_PROMPT.replace("{categories_list}", "Продукты, Авто")
    assert "Продукты, Авто" in prompt
    assert '"amount"' in prompt


def test_fallback_parse_expense():
    parsed = fallback_parse_text("500 Продукты молоко", ["Продукты", "Авто"])
    assert parsed is not None
    assert parsed.amount == 500
    assert parsed.type == "expense"
    assert parsed.category == "Продукты"


def test_fallback_parse_income():
    parsed = fallback_parse_text("+150000 Зарплата", ["Зарплата"])
    assert parsed is not None
    assert parsed.type == "income"
    assert parsed.category == "Зарплата"


def test_inline_currency_is_removed_before_text_classification():
    parser_text, amount, currency, tail = inline_payment_parts("10usd продукты")

    assert parser_text == "10 продукты"
    assert amount == Decimal("10")
    assert currency == "USD"
    assert tail == "продукты"


def test_inline_currency_without_space_is_supported():
    parser_text, amount, currency, tail = inline_payment_parts("10USD")

    assert parser_text == "10"
    assert amount == Decimal("10")
    assert currency == "USD"
    assert tail == ""


def test_bare_category_never_becomes_amount_comment():
    parsed = ParsedTransaction(amount=Decimal("10"), type="expense", category="Продукты", comment="10")

    normalized = normalize_text_comment(parsed, Decimal("10"), "продукты")

    assert normalized.comment is None


def test_receipt_discount_is_not_deducted_twice_when_lines_already_match_total():
    parsed = ParsedTransaction(
        amount=Decimal("1996"), type="expense", category="Дом", receipt_discount=Decimal("2"),
        items=[ReceiptItem(name="товар", amount=Decimal("1996"), category="Дом")],
    )

    result = reconcile_receipt_totals(parsed)

    assert result is not None
    assert result.receipt_discount == Decimal("0")


def test_small_missing_receipt_amount_becomes_visible_adjustment_in_largest_category():
    parsed = ParsedTransaction(
        amount=Decimal("1996"), type="expense", category="Дом",
        items=[
            ReceiptItem(name="шкаф", amount=Decimal("1900"), category="Дом"),
            ReceiptItem(name="еда", amount=Decimal("94"), category="Продукты"),
        ],
    )

    result = reconcile_receipt_totals(parsed)

    assert result is not None
    assert sum(item.amount for item in result.items) == Decimal("1996")
    assert result.items[-1].name == "Корректировка по итогу чека"
    assert result.items[-1].amount == Decimal("2")
    assert result.items[-1].category == "Дом"


def test_large_receipt_discrepancy_is_not_auto_corrected():
    parsed = ParsedTransaction(
        amount=Decimal("1996"), type="expense", category="Дом",
        items=[ReceiptItem(name="товар", amount=Decimal("1990"), category="Дом")],
    )

    result = reconcile_receipt_totals(parsed)

    assert result is not None
    assert len(result.items) == 1


def test_unbalanced_receipt_is_never_rendered_as_actionable_draft():
    html = draft_html(
        "token", Decimal("100"), "expense", "Дом", None,
        [{"name": "товар", "amount": "90", "category": "Дом"}],
        can_retry_receipt=True,
    )

    assert "Не удалось сверить чек" in html
    assert "Распознать заново" in html
    assert "Записать" not in html


async def test_receipt_parser_repairs_a_large_mismatch_before_returning_a_draft(monkeypatch):
    parser = AIParser()
    first = ParsedTransaction(
        amount=Decimal("100"), type="expense", category="Продукты",
        items=[ReceiptItem(name="товар", amount=Decimal("120"), category="Продукты")],
    )
    repaired = ParsedTransaction(
        amount=Decimal("100"), type="expense", category="Продукты",
        items=[ReceiptItem(name="товар", amount=Decimal("100"), item_discount=Decimal("20"), category="Продукты")],
    )

    async def initial(*_args, **_kwargs):
        return first

    async def repair(content, *_args, **_kwargs):
        assert "Предыдущий черновик" in content[0]["text"]
        assert "120" in content[0]["text"]
        return repaired

    monkeypatch.setattr(parser, "parse_images", initial)
    monkeypatch.setattr(parser, "_parse", repair)

    result = await parser.parse_receipt_images([(b"image", "image/jpeg")], ["Продукты"], object())

    assert result == repaired
    assert receipt_totals_match(result)
