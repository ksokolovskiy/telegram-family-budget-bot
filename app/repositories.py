from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Budget, Category, Family, Transaction, User

DEFAULT_EXPENSE_CATEGORIES = ["Продукты", "Авто", "Дом", "Развлечения", "Здоровье", "Транспорт", "Другое"]
DEFAULT_INCOME_CATEGORIES = ["Зарплата", "Подарки", "Подработка", "Другое"]


def current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None) -> User:
    user = await session.get(User, telegram_id)
    if user:
        user.username = username
        return user
    user = User(id=telegram_id, username=username, role="member")
    session.add(user)
    await session.flush()
    return user


async def create_family_for_user(session: AsyncSession, user: User, name: str) -> Family:
    invite_code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10].upper()
    family = Family(name=name, invite_code=invite_code)
    session.add(family)
    await session.flush()
    user.family_id = family.id
    user.role = "owner"
    await seed_default_categories(session, family.id)
    return family


async def join_family(session: AsyncSession, user: User, invite_code: str) -> Family | None:
    family = await session.scalar(select(Family).where(Family.invite_code == invite_code.upper().strip()))
    if not family:
        return None
    user.family_id = family.id
    user.role = "member"
    return family


async def seed_default_categories(session: AsyncSession, family_id: int) -> None:
    for name in DEFAULT_EXPENSE_CATEGORIES:
        session.add(Category(family_id=family_id, name=name, type="expense"))
    for name in DEFAULT_INCOME_CATEGORIES:
        session.add(Category(family_id=family_id, name=name, type="income"))


async def list_categories(session: AsyncSession, family_id: int, tx_type: str | None = None) -> list[Category]:
    stmt: Select[tuple[Category]] = select(Category).where(Category.family_id == family_id)
    if tx_type:
        stmt = stmt.where(Category.type == tx_type)
    return list(await session.scalars(stmt.order_by(Category.type, Category.name)))


async def find_category(session: AsyncSession, family_id: int, name: str, tx_type: str) -> Category | None:
    return await session.scalar(
        select(Category).where(
            Category.family_id == family_id,
            func.lower(Category.name) == name.lower(),
            Category.type == tx_type,
        )
    )


async def create_category(session: AsyncSession, family_id: int, name: str, tx_type: str) -> Category:
    category = Category(family_id=family_id, name=name.strip().title(), type=tx_type)
    session.add(category)
    await session.flush()
    return category


async def add_transaction(
    session: AsyncSession,
    family_id: int,
    user_id: int,
    category: Category,
    amount: Decimal,
    tx_type: str,
    comment: str | None,
) -> Transaction:
    tx = Transaction(
        family_id=family_id,
        user_id=user_id,
        category_id=category.id,
        amount=amount,
        type=tx_type,
        comment=comment,
    )
    session.add(tx)
    await session.flush()
    return tx


async def upsert_budget(session: AsyncSession, family_id: int, category_id: int, amount: Decimal, period: str | None = None) -> None:
    stmt = insert(Budget).values(
        family_id=family_id,
        category_id=category_id,
        amount=amount,
        period=period or current_period(),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_budget_family_category_period",
        set_={"amount": stmt.excluded.amount},
    )
    await session.execute(stmt)
