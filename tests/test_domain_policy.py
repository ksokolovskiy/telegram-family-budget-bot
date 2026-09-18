from app.models import User
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import Category
from app.repositories import (
    AuthorizationError,
    can_edit_family_data,
    can_manage_family,
    has_family_role,
    next_recurrence_at,
    require_family_data_edit,
    validate_role,
    validate_split_lines,
)


def make_user(role: str, family_id: int | None = 1) -> User:
    return User(id=1, username="test", role=role, family_id=family_id)


def test_family_role_hierarchy_is_explicit() -> None:
    assert can_manage_family(make_user("owner"))
    assert can_edit_family_data(make_user("owner"))
    assert not can_manage_family(make_user("editor"))
    assert can_edit_family_data(make_user("editor"))
    assert not can_edit_family_data(make_user("viewer"))
    assert has_family_role(make_user("viewer"), "viewer")


def test_role_requires_family_and_valid_role() -> None:
    assert not has_family_role(make_user("owner", None), "viewer")
    assert not has_family_role(make_user("member"), "viewer")


def test_role_validation_canonicalizes_and_rejects_unknown() -> None:
    assert validate_role(" Editor ") == "editor"
    try:
        validate_role("member")
    except ValueError as error:
        assert "owner" in str(error)
    else:
        raise AssertionError("legacy member role must not authorize new paths")


def test_mutation_guard_requires_editor_in_same_family() -> None:
    require_family_data_edit(make_user("editor", 10), 10)
    with pytest.raises(AuthorizationError):
        require_family_data_edit(make_user("viewer", 10), 10)
    with pytest.raises(AuthorizationError):
        require_family_data_edit(make_user("owner", 11), 10)


def test_recurrence_handles_short_months_and_rejects_unknown_cadence() -> None:
    start = datetime(2026, 1, 31, 8, tzinfo=timezone.utc)
    assert next_recurrence_at(start, "monthly") == datetime(2026, 2, 28, 8, tzinfo=timezone.utc)
    assert next_recurrence_at(start, "weekly") == datetime(2026, 2, 7, 8, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        next_recurrence_at(start, "daily")


def test_split_requires_multiple_positive_lines() -> None:
    category = Category(id=1, family_id=1, name="Продукты", type="expense")
    with pytest.raises(ValueError):
        validate_split_lines([(category, Decimal("1.00"), None)])
    with pytest.raises(ValueError):
        validate_split_lines([(category, Decimal("1.00"), None), (category, Decimal("0"), None)])
    assert len(validate_split_lines([(category, Decimal("1"), None), (category, Decimal("2"), None)])) == 2
