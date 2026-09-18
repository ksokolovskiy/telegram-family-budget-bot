from decimal import Decimal

from app.rich_bot import receipt_category_groups


def test_receipt_items_are_aggregated_by_category_in_first_seen_order():
    groups = receipt_category_groups(
        [
            {"name": "коврик", "amount": "173", "category": "Дом"},
            {"name": "батончик", "amount": "12", "category": "Продукты"},
            {"name": "полка", "amount": "390", "category": "Дом"},
            {"name": "варенье", "amount": "23", "category": "Продукты"},
        ]
    )

    assert [(item["category"], item["amount"], item["count"]) for item in groups] == [
        ("Дом", Decimal("563"), 2),
        ("Продукты", Decimal("35"), 2),
    ]


def test_whole_receipt_discount_is_allocated_proportionally_with_no_rounding_loss():
    groups = receipt_category_groups(
        [
            {"name": "дом", "amount": "563", "category": "Дом"},
            {"name": "еда", "amount": "35", "category": "Продукты"},
        ],
        Decimal("60"),
    )

    assert [(item["category"], item["discount"], item["amount"]) for item in groups] == [
        ("Дом", Decimal("56.49"), Decimal("506.51")),
        ("Продукты", Decimal("3.51"), Decimal("31.49")),
    ]
    assert sum((item["amount"] for item in groups), Decimal("0")) == Decimal("538.00")
