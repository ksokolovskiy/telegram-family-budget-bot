from __future__ import annotations

from datetime import date, datetime
from typing import Any
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text(), nullable=False)
    encryption_version: Mapped[str | None] = mapped_column(String(32))
    encrypted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Family(Base):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    invite_code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Jerusalem", server_default="Asia/Jerusalem")
    currency: Mapped[str] = mapped_column(String(3), default="ILS", server_default="ILS")
    receipt_retention_days: Mapped[int] = mapped_column(default=365, server_default="365")
    notify_members_on_transactions: Mapped[bool] = mapped_column(default=True, server_default="true")
    # AI credentials are deliberately family-scoped: a family pays only for
    # requests made for its own budget and receipt images.
    openai_api_key: Mapped[str | None] = mapped_column(Text())
    openai_api_key_encryption_version: Mapped[str | None] = mapped_column(String(32))
    openai_api_key_encrypted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    openai_model: Mapped[str] = mapped_column(String(120), default="gpt-5.4", server_default="gpt-5.4")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    users: Mapped[list[User]] = relationship(back_populates="family")
    invites: Mapped[list[FamilyInvite]] = relationship(back_populates="family", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    family_id: Mapped[int | None] = mapped_column(ForeignKey("families.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(20), default="viewer", server_default="viewer")

    family: Mapped[Family | None] = relationship(back_populates="users")


class FamilyInvite(Base):
    """A rotatable, revocable invitation.  Codes never grant elevated roles."""

    __tablename__ = "family_invites"
    __table_args__ = (Index("ix_family_invites_family_active", "family_id", "revoked_at", "expires_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer", server_default="viewer")
    created_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    revoked_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    uses_count: Mapped[int] = mapped_column(default=0, server_default="0")
    max_uses: Mapped[int | None] = mapped_column()

    family: Mapped[Family] = relationship(back_populates="invites")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("family_id", "name", "type", name="uq_category_family_name_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int | None] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(default=True)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_family_date", "family_id", "date"),
        Index("ix_transactions_family_category_date", "family_id", "category_id", "date"),
        Index("ix_transactions_family_active_date", "family_id", "deleted_at", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    source_currency: Mapped[str | None] = mapped_column(String(3))
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    exchange_rate_date: Mapped[date | None] = mapped_column()
    exchange_rate_source: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text())
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_mime_type: Mapped[str | None] = mapped_column(String(120))
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
    updated_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    deletion_reason: Mapped[str | None] = mapped_column(Text())
    group_id: Mapped[int | None] = mapped_column(ForeignKey("transaction_groups.id", ondelete="SET NULL"))
    receipt_retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    receipt_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TransactionAudit(Base):
    """Append-only audit trail for manual updates and soft deletion."""

    __tablename__ = "transaction_audits"
    __table_args__ = (Index("ix_transaction_audits_transaction_created", "transaction_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(20))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TransactionGroup(Base):
    """Groups child transactions created by a split payment or a transfer."""

    __tablename__ = "transaction_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20))  # split or transfer
    comment: Mapped[str | None] = mapped_column(Text())
    created_by_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Account(Base):
    """Optional family account/wallet used to represent transfers."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("family_id", "name", name="uq_account_family_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("transaction_groups.id", ondelete="CASCADE"), unique=True)
    from_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    to_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RecurringTransaction(Base):
    __tablename__ = "recurring_transactions"
    __table_args__ = (Index("ix_recurring_transactions_due", "family_id", "is_active", "next_run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    type: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text())
    cadence: Mapped[str] = mapped_column(String(20))  # weekly or monthly
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("family_id", "category_id", "period", name="uq_budget_family_category_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    period: Mapped[str] = mapped_column(String(7))


class InteractiveScreen(Base):
    """A server-side, user-bound state for an editable rich Telegram message."""

    __tablename__ = "interactive_screens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column()
    revision: Mapped[int] = mapped_column(default=1)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TransactionDraft(Base):
    """Short-lived confirmation data; survives restarts and is bound to its author."""

    __tablename__ = "transaction_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column()
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    source_currency: Mapped[str | None] = mapped_column(String(3))
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    exchange_rate_date: Mapped[date | None] = mapped_column()
    exchange_rate_source: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(120))
    comment: Mapped[str | None] = mapped_column(Text())
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_mime_type: Mapped[str | None] = mapped_column(String(120))
    items: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    receipt_discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
