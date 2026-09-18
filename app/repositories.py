from __future__ import annotations

import secrets
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Literal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    Budget,
    Category,
    Family,
    FamilyInvite,
    RecurringTransaction,
    Transaction,
    TransactionAudit,
    TransactionGroup,
    Transfer,
    User,
)

DEFAULT_EXPENSE_CATEGORIES = ["Продукты", "Авто", "Дом", "Развлечения", "Здоровье", "Транспорт", "Другое"]
DEFAULT_INCOME_CATEGORIES = ["Зарплата", "Подарки", "Подработка", "Другое"]
FamilyRole = Literal["owner", "editor", "viewer"]
VALID_ROLES = frozenset({"owner", "editor", "viewer"})
ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}


class AuthorizationError(PermissionError):
    """Raised by handler-facing guards before a family mutation is made."""


def validate_role(role: str) -> FamilyRole:
    """Return a canonical role or fail before a database write."""
    normalized = role.strip().lower()
    if normalized not in VALID_ROLES:
        raise ValueError("Роль должна быть owner, editor или viewer.")
    return normalized  # type: ignore[return-value]


def has_family_role(user: User | None, minimum: FamilyRole) -> bool:
    """Authorization-ready role check; a user without a family never has access."""
    return bool(
        user
        and user.family_id is not None
        and user.role in ROLE_RANK
        and ROLE_RANK[user.role] >= ROLE_RANK[minimum]
    )


def can_manage_family(user: User | None) -> bool:
    return has_family_role(user, "owner")


def can_edit_family_data(user: User | None) -> bool:
    return has_family_role(user, "editor")


def require_family_role(user: User | None, family_id: int, minimum: FamilyRole) -> None:
    if user is None or user.family_id != family_id or not has_family_role(user, minimum):
        raise AuthorizationError("Недостаточно прав для изменения данных семьи.")


def require_family_data_edit(user: User | None, family_id: int) -> None:
    require_family_role(user, family_id, "editor")


def require_family_management(user: User | None, family_id: int) -> None:
    require_family_role(user, family_id, "owner")


def _new_invite_code() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24].upper()


def current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None) -> User:
    user = await session.get(User, telegram_id)
    if user:
        user.username = username
        return user
    user = User(id=telegram_id, username=username, role="viewer")
    session.add(user)
    await session.flush()
    return user


async def create_family_for_user(session: AsyncSession, user: User, name: str) -> Family:
    invite_code = _new_invite_code()
    family = Family(name=name, invite_code=invite_code)
    session.add(family)
    await session.flush()
    user.family_id = family.id
    user.role = "owner"
    # The legacy field remains as a display-compatible copy of the first
    # lifecycle invite. New joins always resolve the invite record first.
    session.add(
        FamilyInvite(
            family_id=family.id,
            code=invite_code,
            role="viewer",
            created_by_id=user.id,
        )
    )
    await seed_default_categories(session, family.id)
    return family


async def join_family(session: AsyncSession, user: User, invite_code: str) -> Family | None:
    code = invite_code.upper().strip()
    now = datetime.now(timezone.utc)
    invite = await session.scalar(
        select(FamilyInvite).where(
            FamilyInvite.code == code,
            FamilyInvite.revoked_at.is_(None),
            (FamilyInvite.expires_at.is_(None)) | (FamilyInvite.expires_at > now),
        )
    )
    if invite and invite.max_uses is not None and invite.uses_count >= invite.max_uses:
        invite = None
    if invite:
        family = await session.get(Family, invite.family_id)
        if family is None:
            return None
        invite.uses_count += 1
        user.family_id = family.id
        user.role = validate_role(invite.role)
        return family

    # Compatibility with codes issued before the invite lifecycle was added.
    # Once a family has any lifecycle invite (including revoked ones), legacy
    # codes are deliberately disabled, so rotation/revocation takes effect.
    family = await session.scalar(select(Family).where(Family.invite_code == code))
    if family is None:
        return None
    has_lifecycle_invites = await session.scalar(
        select(FamilyInvite.id).where(FamilyInvite.family_id == family.id).limit(1)
    )
    if has_lifecycle_invites is not None:
        return None
    user.family_id = family.id
    user.role = "viewer"
    return family


async def create_family_invite(
    session: AsyncSession,
    family_id: int,
    created_by_id: int,
    *,
    role: FamilyRole = "viewer",
    expires_at: datetime | None = None,
    max_uses: int | None = None,
) -> FamilyInvite:
    if max_uses is not None and max_uses < 1:
        raise ValueError("max_uses должен быть не меньше 1.")
    invite = FamilyInvite(
        family_id=family_id,
        code=_new_invite_code(),
        role=validate_role(role),
        created_by_id=created_by_id,
        expires_at=expires_at,
        max_uses=max_uses,
    )
    session.add(invite)
    await session.flush()
    return invite


async def revoke_family_invite(session: AsyncSession, family_id: int, invite_id: int, revoked_by_id: int) -> bool:
    invite = await session.scalar(select(FamilyInvite).where(FamilyInvite.id == invite_id, FamilyInvite.family_id == family_id))
    if invite is None or invite.revoked_at is not None:
        return False
    invite.revoked_at = datetime.now(timezone.utc)
    invite.revoked_by_id = revoked_by_id
    return True


async def rotate_family_invite(session: AsyncSession, family_id: int, actor_id: int) -> FamilyInvite:
    """Revoke currently active lifecycle invites and issue one new viewer link."""
    now = datetime.now(timezone.utc)
    invites = await session.scalars(
        select(FamilyInvite).where(FamilyInvite.family_id == family_id, FamilyInvite.revoked_at.is_(None))
    )
    for invite in invites:
        invite.revoked_at = now
        invite.revoked_by_id = actor_id
    fresh = await create_family_invite(session, family_id, actor_id)
    family = await session.get(Family, family_id)
    if family is not None:
        family.invite_code = fresh.code
    return fresh


async def update_family_preferences(
    session: AsyncSession, family: Family, *, timezone_name: str | None = None, currency: str | None = None
) -> Family:
    if timezone_name is not None:
        # ZoneInfo is deliberately validated by the caller/UI too; keeping this
        # simple permits all valid IANA zone names without a hard-coded list.
        from zoneinfo import ZoneInfo

        ZoneInfo(timezone_name)
        family.timezone = timezone_name
    if currency is not None:
        normalized = currency.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("Валюта должна быть трёхбуквенным ISO-кодом.")
        family.currency = normalized
    return family


async def seed_default_categories(session: AsyncSession, family_id: int) -> None:
    for name in DEFAULT_EXPENSE_CATEGORIES:
        session.add(Category(family_id=family_id, name=name, type="expense"))
    for name in DEFAULT_INCOME_CATEGORIES:
        session.add(Category(family_id=family_id, name=name, type="income"))


async def list_categories(
    session: AsyncSession, family_id: int, tx_type: str | None = None, active_only: bool = True
) -> list[Category]:
    stmt: Select[tuple[Category]] = select(Category).where(Category.family_id == family_id)
    if active_only:
        stmt = stmt.where(Category.is_active.is_(True))
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


async def archive_category(session: AsyncSession, category: Category) -> None:
    category.is_active = False


async def category_is_referenced(session: AsyncSession, category_id: int) -> bool:
    transaction_exists = await session.scalar(select(Transaction.id).where(Transaction.category_id == category_id).limit(1))
    budget_exists = await session.scalar(select(Budget.id).where(Budget.category_id == category_id).limit(1))
    return transaction_exists is not None or budget_exists is not None


async def delete_category_if_unused(session: AsyncSession, category: Category) -> bool:
    """Hard delete only truly unused categories; history must remain referentially intact."""
    if await category_is_referenced(session, category.id):
        return False
    await session.delete(category)
    return True


async def add_transaction(
    session: AsyncSession,
    family_id: int,
    user_id: int | None,
    category: Category,
    amount: Decimal,
    tx_type: str,
    comment: str | None,
    receipt_file_id: str | None = None,
    receipt_mime_type: str | None = None,
    source_amount: Decimal | None = None,
    source_currency: str | None = None,
    exchange_rate: Decimal | None = None,
    exchange_rate_date: date | None = None,
    exchange_rate_source: str | None = None,
) -> Transaction:
    family = await session.get(Family, family_id)
    retention_until = None
    if receipt_file_id and family is not None and family.receipt_retention_days > 0:
        retention_until = datetime.now(timezone.utc) + timedelta(days=family.receipt_retention_days)
    tx = Transaction(
        family_id=family_id,
        user_id=user_id,
        category_id=category.id,
        amount=amount,
        type=tx_type,
        comment=comment,
        receipt_file_id=receipt_file_id,
        receipt_mime_type=receipt_mime_type,
        receipt_retention_until=retention_until,
        source_amount=source_amount,
        source_currency=source_currency,
        exchange_rate=exchange_rate,
        exchange_rate_date=exchange_rate_date,
        exchange_rate_source=exchange_rate_source,
    )
    session.add(tx)
    await session.flush()
    session.add(
        TransactionAudit(
            transaction_id=tx.id,
            family_id=family_id,
            actor_id=user_id,
            action="created",
            after=_transaction_snapshot(tx),
        )
    )
    return tx


def _transaction_snapshot(tx: Transaction) -> dict[str, str | int | None]:
    return {
        "amount": str(tx.amount),
        "type": tx.type,
        "category_id": tx.category_id,
        "comment": tx.comment,
        "receipt_file_id": tx.receipt_file_id,
        "receipt_mime_type": tx.receipt_mime_type,
        "date": tx.date.isoformat() if tx.date else None,
        "group_id": tx.group_id,
        "receipt_retention_until": tx.receipt_retention_until.isoformat() if tx.receipt_retention_until else None,
    }


async def edit_transaction(
    session: AsyncSession,
    tx: Transaction,
    actor_id: int,
    *,
    amount: Decimal | None = None,
    category_id: int | None = None,
    tx_type: str | None = None,
    comment: str | None = None,
    date: datetime | None = None,
) -> Transaction:
    if tx.deleted_at is not None:
        raise ValueError("Удалённую операцию нельзя редактировать.")
    before = _transaction_snapshot(tx)
    if amount is not None:
        tx.amount = amount
    if category_id is not None:
        tx.category_id = category_id
    if tx_type is not None:
        tx.type = tx_type
    if comment is not None:
        tx.comment = comment
    if date is not None:
        tx.date = date
    tx.updated_by_id = actor_id
    await session.flush()
    session.add(TransactionAudit(transaction_id=tx.id, family_id=tx.family_id, actor_id=actor_id, action="updated", before=before, after=_transaction_snapshot(tx)))
    return tx


async def delete_transaction(session: AsyncSession, tx: Transaction, actor_id: int, reason: str | None = None) -> bool:
    if tx.deleted_at is not None:
        return False
    before = _transaction_snapshot(tx)
    tx.deleted_at = datetime.now(timezone.utc)
    tx.deleted_by_id = actor_id
    tx.deletion_reason = reason
    await session.flush()
    session.add(TransactionAudit(transaction_id=tx.id, family_id=tx.family_id, actor_id=actor_id, action="deleted", before=before, after=_transaction_snapshot(tx)))
    return True


async def set_receipt_retention_policy(session: AsyncSession, family: Family, retention_days: int) -> Family:
    """Set how long our DB may retain Telegram file IDs (0 disables new retention)."""
    if not 0 <= retention_days <= 3650:
        raise ValueError("Срок хранения чека должен быть от 0 до 3650 дней.")
    family.receipt_retention_days = retention_days
    return family


async def list_receipts_due_for_purge(session: AsyncSession, *, now: datetime | None = None, limit: int = 100) -> list[Transaction]:
    moment = now or datetime.now(timezone.utc)
    stmt = (
        select(Transaction)
        .where(
            Transaction.receipt_file_id.is_not(None),
            Transaction.receipt_purged_at.is_(None),
            Transaction.receipt_retention_until.is_not(None),
            Transaction.receipt_retention_until <= moment,
        )
        .order_by(Transaction.receipt_retention_until)
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def purge_transaction_receipt(session: AsyncSession, tx: Transaction, actor_id: int | None = None) -> bool:
    """Erase the locally retained Telegram media reference, never the transaction itself."""
    if tx.receipt_file_id is None or tx.receipt_purged_at is not None:
        return False
    before = _transaction_snapshot(tx)
    tx.receipt_file_id = None
    tx.receipt_mime_type = None
    tx.receipt_purged_at = datetime.now(timezone.utc)
    await session.flush()
    session.add(
        TransactionAudit(
            transaction_id=tx.id,
            family_id=tx.family_id,
            actor_id=actor_id,
            action="receipt_purged",
            before=before,
            after=_transaction_snapshot(tx),
        )
    )
    return True


async def list_transactions(
    session: AsyncSession,
    family_id: int,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_deleted: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Transaction]:
    filters = [Transaction.family_id == family_id]
    if not include_deleted:
        filters.append(Transaction.deleted_at.is_(None))
    if date_from:
        filters.append(Transaction.date >= date_from)
    if date_to:
        filters.append(Transaction.date < date_to)
    return list(await session.scalars(select(Transaction).where(and_(*filters)).order_by(Transaction.date.desc(), Transaction.id.desc()).limit(limit).offset(offset)))


def validate_split_lines(lines: Iterable[tuple[Category, Decimal, str | None]]) -> list[tuple[Category, Decimal, str | None]]:
    normalized = list(lines)
    if len(normalized) < 2:
        raise ValueError("Split-платёж должен содержать минимум две позиции.")
    if any(amount <= 0 for _, amount, _ in normalized):
        raise ValueError("Сумма каждой позиции должна быть больше нуля.")
    return normalized


async def create_split_payment(
    session: AsyncSession,
    family_id: int,
    user_id: int,
    tx_type: str,
    lines: Iterable[tuple[Category, Decimal, str | None]],
    *,
    comment: str | None = None,
    receipt_file_id: str | None = None,
    receipt_mime_type: str | None = None,
) -> tuple[TransactionGroup, list[Transaction]]:
    """Persist an arbitrary category split as one group of normal transactions."""
    normalized = validate_split_lines(lines)
    if any(category.family_id != family_id or category.type != tx_type for category, _, _ in normalized):
        raise ValueError("Все категории split-платежа должны принадлежать семье и иметь верный тип.")
    group = TransactionGroup(family_id=family_id, kind="split", comment=comment, created_by_id=user_id)
    session.add(group)
    await session.flush()
    transactions: list[Transaction] = []
    for category, amount, line_comment in normalized:
        tx = await add_transaction(
            session,
            family_id,
            user_id,
            category,
            amount,
            tx_type,
            line_comment or comment,
            receipt_file_id,
            receipt_mime_type,
        )
        tx.group_id = group.id
        transactions.append(tx)
    return group, transactions


async def create_account(session: AsyncSession, family_id: int, name: str, currency: str) -> Account:
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise ValueError("Валюта счёта должна быть трёхбуквенным ISO-кодом.")
    account = Account(family_id=family_id, name=name.strip(), currency=normalized_currency)
    session.add(account)
    await session.flush()
    return account


async def create_transfer(
    session: AsyncSession,
    family_id: int,
    actor_id: int,
    from_account: Account,
    to_account: Account,
    amount: Decimal,
    *,
    comment: str | None = None,
    date: datetime | None = None,
) -> Transfer:
    if amount <= 0:
        raise ValueError("Сумма перевода должна быть больше нуля.")
    if from_account.id == to_account.id:
        raise ValueError("Счета перевода должны различаться.")
    if from_account.family_id != family_id or to_account.family_id != family_id:
        raise ValueError("Счета должны принадлежать одной семье.")
    if from_account.currency != to_account.currency:
        raise ValueError("Переводы между разными валютами пока требуют отдельной конвертации.")
    group = TransactionGroup(family_id=family_id, kind="transfer", comment=comment, created_by_id=actor_id)
    session.add(group)
    await session.flush()
    transfer = Transfer(
        family_id=family_id,
        group_id=group.id,
        from_account_id=from_account.id,
        to_account_id=to_account.id,
        amount=amount,
        date=date or datetime.now(timezone.utc),
    )
    session.add(transfer)
    await session.flush()
    return transfer


def next_recurrence_at(current: datetime, cadence: str) -> datetime:
    if cadence == "weekly":
        return current + timedelta(days=7)
    if cadence == "monthly":
        year = current.year + (current.month == 12)
        month = 1 if current.month == 12 else current.month + 1
        return current.replace(year=year, month=month, day=min(current.day, monthrange(year, month)[1]))
    raise ValueError("Поддерживаются периодичности weekly и monthly.")


async def create_recurring_transaction(
    session: AsyncSession,
    family_id: int,
    user_id: int,
    category: Category,
    amount: Decimal,
    tx_type: str,
    cadence: str,
    next_run_at: datetime,
    *,
    comment: str | None = None,
    ends_at: datetime | None = None,
) -> RecurringTransaction:
    if amount <= 0:
        raise ValueError("Сумма регулярной операции должна быть больше нуля.")
    next_recurrence_at(next_run_at, cadence)  # validates cadence
    if category.family_id != family_id or category.type != tx_type:
        raise ValueError("Категория регулярной операции не подходит семье или типу.")
    recurring = RecurringTransaction(
        family_id=family_id,
        user_id=user_id,
        category_id=category.id,
        amount=amount,
        type=tx_type,
        comment=comment,
        cadence=cadence,
        next_run_at=next_run_at,
        ends_at=ends_at,
    )
    session.add(recurring)
    await session.flush()
    return recurring


async def list_due_recurring_transactions(session: AsyncSession, *, now: datetime | None = None, limit: int = 100) -> list[RecurringTransaction]:
    moment = now or datetime.now(timezone.utc)
    stmt = (
        select(RecurringTransaction)
        .where(
            RecurringTransaction.is_active.is_(True),
            RecurringTransaction.next_run_at <= moment,
            (RecurringTransaction.ends_at.is_(None)) | (RecurringTransaction.ends_at >= RecurringTransaction.next_run_at),
        )
        .order_by(RecurringTransaction.next_run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(await session.scalars(stmt))


async def materialize_recurring_transaction(session: AsyncSession, recurring: RecurringTransaction) -> Transaction:
    if not recurring.is_active:
        raise ValueError("Неактивное правило нельзя выполнить.")
    category = await session.get(Category, recurring.category_id)
    if category is None:
        raise ValueError("Категория регулярной операции удалена.")
    tx = await add_transaction(
        session,
        recurring.family_id,
        recurring.user_id,
        category,
        recurring.amount,
        recurring.type,
        recurring.comment,
    )
    tx.date = recurring.next_run_at
    next_at = next_recurrence_at(recurring.next_run_at, recurring.cadence)
    if recurring.ends_at is not None and next_at > recurring.ends_at:
        recurring.is_active = False
    else:
        recurring.next_run_at = next_at
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


async def list_budgets(session: AsyncSession, family_id: int, period: str | None = None) -> list[Budget]:
    return list(
        await session.scalars(
            select(Budget).where(Budget.family_id == family_id, Budget.period == (period or current_period())).order_by(Budget.category_id)
        )
    )


async def copy_budgets_from_period(
    session: AsyncSession, family_id: int, source_period: str, target_period: str
) -> int:
    """Copy a family's plans without overwriting plans already set for target."""
    if source_period == target_period:
        raise ValueError("Исходный и целевой периоды должны различаться.")
    source = await session.scalars(select(Budget).where(Budget.family_id == family_id, Budget.period == source_period))
    copied = 0
    for budget in source:
        exists = await session.scalar(
            select(Budget.id).where(Budget.family_id == family_id, Budget.category_id == budget.category_id, Budget.period == target_period)
        )
        if exists is None:
            session.add(Budget(family_id=family_id, category_id=budget.category_id, amount=budget.amount, period=target_period))
            copied += 1
    return copied
