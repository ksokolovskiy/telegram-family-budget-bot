from app.ai import fallback_parse_text


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
