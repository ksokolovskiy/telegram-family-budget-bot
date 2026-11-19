from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import installment_management as plans


def row(i, day, deleted=False):
    return SimpleNamespace(
        id=i,
        date=datetime.combine(day, datetime.min.time(), timezone.utc),
        deleted_at=datetime.now(timezone.utc) if deleted else None,
        amount=Decimal("10.00"),
        category_id=1,
        comment="Покупка",
    )


def test_remaining_uses_family_day_and_excludes_deleted():
    rows = [
        row(1, date(2026, 11, 18)),
        row(2, date(2026, 11, 19)),
        row(3, date(2026, 11, 20), True),
    ]
    assert [tx.id for tx in plans.remaining(rows, "Asia/Jerusalem", date(2026, 11, 19))] == [2]


def test_remaining_can_be_combined_into_one_payment():
    assert plans.schedule("100", 1, date(2027, 1, 31)) == [(date(2027, 1, 31), Decimal("100"))]


@pytest.mark.parametrize("kind", ["cancel", "comment", "category"])
async def test_bulk_change_never_touches_past_or_deleted(monkeypatch, kind):
    rows = [
        row(1, date(2020, 1, 1)),
        row(2, date(2090, 1, 1)),
        row(3, date(2090, 2, 1)),
        row(4, date(2090, 3, 1), True),
    ]
    family = SimpleNamespace(currency="ILS", timezone="Asia/Jerusalem")
    actor = SimpleNamespace(family_id=1, role="owner")
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=actor),
        get=AsyncMock(
            return_value=SimpleNamespace(family_id=1, type="expense", is_active=True, id=1)
        ),
    )
    monkeypatch.setattr(plans, "load", AsyncMock(return_value=(object(), rows, family)))
    monkeypatch.setattr(
        plans, "api", lambda: SimpleNamespace(can_manage_family=lambda u: u.role == "owner")
    )
    edit = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(plans, "edit_transaction", edit)
    monkeypatch.setattr(plans, "delete_transaction", delete)
    change = dict(
        kind=kind,
        signature=plans.fingerprint(rows),
        remaining_ids=[2, 3],
        currency="ILS",
        zone="Asia/Jerusalem",
        category_id=1,
        comment="New",
    )
    await plans.apply_change(session, 1, 1, 42, change)
    calls = delete.await_args_list if kind == "cancel" else edit.await_args_list
    assert [call.args[1].id for call in calls] == [2, 3]


async def test_changed_schedule_rejects_stale_confirmation(monkeypatch):
    rows = [row(1, date(2090, 1, 1))]
    family = SimpleNamespace(currency="ILS", timezone="Asia/Jerusalem")
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(family_id=1, role="owner"))
    )
    monkeypatch.setattr(plans, "api", lambda: SimpleNamespace(can_manage_family=lambda u: True))
    monkeypatch.setattr(plans, "load", AsyncMock(return_value=(object(), rows, family)))
    delete = AsyncMock()
    monkeypatch.setattr(plans, "delete_transaction", delete)
    change = dict(
        kind="cancel",
        signature="outdated",
        currency="ILS",
        zone="Asia/Jerusalem",
        remaining_ids=[1],
    )
    with pytest.raises(ValueError, match="изменились"):
        await plans.apply_change(session, 1, 1, 42, change)
    delete.assert_not_awaited()


async def test_viewer_cannot_mutate_group(monkeypatch):
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(family_id=1, role="viewer"))
    )
    monkeypatch.setattr(plans, "api", lambda: SimpleNamespace(can_manage_family=lambda u: False))
    with pytest.raises(ValueError, match="прав"):
        await plans.apply_change(session, 1, 1, 42, {})
