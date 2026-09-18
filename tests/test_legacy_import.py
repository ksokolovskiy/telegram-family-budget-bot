from app.legacy_import import MAPPING


def test_rental_income_and_rent_expense_are_not_collapsed():
    assert MAPPING["Аренда квартиры"] == ("income", "Аренда")
    assert MAPPING["Аренда"] == ("expense", "Аренда")
