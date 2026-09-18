from decimal import Decimal

from app.ai import ParsedTransaction, ReceiptItem, SYSTEM_PROMPT, fallback_parse_text, reconcile_receipt_totals


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


def test_receipt_discount_is_not_deducted_twice_when_lines_already_match_total():
    parsed = ParsedTransaction(
        amount=Decimal("1996"), type="expense", category="Дом", receipt_discount=Decimal("2"),
        items=[ReceiptItem(name="товар", amount=Decimal("1996"), category="Дом")],
    )

    result = reconcile_receipt_totals(parsed)

    assert result is not None
    assert result.receipt_discount == Decimal("0")
