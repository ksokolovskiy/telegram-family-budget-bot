from decimal import Decimal

from app.ai import ParsedTransaction, ReceiptItem, SYSTEM_PROMPT, fallback_parse_text, reconcile_receipt_totals
from app.rich_bot import draft_html


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
