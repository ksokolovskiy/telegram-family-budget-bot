from datetime import date
from decimal import Decimal

import pytest

from app.installments import schedule


def test_equal_monthly_payments_preserve_total():
    result = schedule('1200', 6, date(2026, 11, 19))
    assert [amount for _, amount in result] == [Decimal('200')] * 6
    assert result[-1][0] == date(2027, 4, 19)


def test_rounding_never_loses_agorot():
    result = schedule('100', 3, date(2026, 11, 19))
    assert [amount for _, amount in result] == [Decimal('33.34'), Decimal('33.33'), Decimal('33.33')]
    assert sum(amount for _, amount in result) == Decimal('100')


def test_month_end_does_not_drift_and_leap_year_is_supported():
    assert [day for day, _ in schedule('300', 3, date(2028, 1, 31))] == [date(2028, 1, 31), date(2028, 2, 29), date(2028, 3, 31)]


@pytest.mark.parametrize('total,count', [('0', 2), ('-1', 2), ('NaN', 2), ('Infinity', 2), ('1.001', 2), ('1', 1), ('1', 37), ('.01', 3)])
def test_invalid_schedule_rejected(total, count):
    with pytest.raises(ValueError):
        schedule(total, count, date(2026, 1, 1))
