from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Budget, Category, Transaction
from app.repositories import current_period


def money(value: Decimal | int | float | None) -> str:
    amount = Decimal(value or 0)
    return f"{amount:,.0f}".replace(",", " ") + " ₽"


def period_bounds(kind: str) -> tuple[datetime, datetime, str]:
    now = datetime.now(timezone.utc)
    if kind == "year":
        start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
        end = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        return start, end, str(now.year)
    if kind == "quarter":
        q_month = ((now.month - 1) // 3) * 3 + 1
        next_month = q_month + 3
        start = datetime(now.year, q_month, 1, tzinfo=timezone.utc)
        end_year = now.year + (1 if next_month > 12 else 0)
        end_month = next_month if next_month <= 12 else 1
        end = datetime(end_year, end_month, 1, tzinfo=timezone.utc)
        return start, end, f"Q{((now.month - 1) // 3) + 1} {now.year}"
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    end_year = now.year + (1 if now.month == 12 else 0)
    end_month = 1 if now.month == 12 else now.month + 1
    end = datetime(end_year, end_month, 1, tzinfo=timezone.utc)
    return start, end, current_period()


async def build_report(session: AsyncSession, family_id: int, kind: str = "month") -> str:
    start, end, label = period_bounds(kind)
    fact_rows = await session.execute(
        select(Category.name, Transaction.type, func.coalesce(func.sum(Transaction.amount), 0))
        .join(Category, Category.id == Transaction.category_id)
        .where(Transaction.family_id == family_id, Transaction.date >= start, Transaction.date < end)
        .group_by(Category.name, Transaction.type)
        .order_by(Category.name)
    )
    facts = {(name, tx_type): Decimal(total) for name, tx_type, total in fact_rows}

    plan_rows = await session.execute(
        select(Category.name, Category.type, func.coalesce(func.sum(Budget.amount), 0))
        .join(Category, Category.id == Budget.category_id)
        .where(and_(Budget.family_id == family_id, Budget.period == current_period()))
        .group_by(Category.name, Category.type)
        .order_by(Category.name)
    )
    plans = {(name, tx_type): Decimal(total) for name, tx_type, total in plan_rows}

    income_plan = sum(value for (_, tx_type), value in plans.items() if tx_type == "income")
    income_fact = sum(value for (_, tx_type), value in facts.items() if tx_type == "income")
    expense_fact = sum(value for (_, tx_type), value in facts.items() if tx_type == "expense")

    lines = [
        f"📊 *Отчет по бюджету за {label}*",
        "",
        "💰 *ДОХОДЫ:*",
        f"План: {money(income_plan)} | Факт: {money(income_fact)}",
        "",
        "📉 *РАСХОДЫ ПО КАТЕГОРИЯМ:*",
    ]
    expense_keys = sorted({name for name, tx_type in plans | facts if tx_type == "expense"})
    if not expense_keys:
        lines.append("Пока нет расходов за период.")
    for name in expense_keys:
        plan = plans.get((name, "expense"), Decimal(0))
        fact = facts.get((name, "expense"), Decimal(0))
        delta = plan - fact
        marker = "🟢" if delta > 1000 else "🟡" if delta >= 0 else "🔴"
        status = "Остаток" if delta >= 0 else "Перерасход"
        lines.append(f"• {name}: План: {money(plan)} | Факт: {money(fact)} ({status}: {money(delta)}) {marker}")

    available = income_fact - expense_fact
    lines.extend(["", "💵 *ИТОГ ЗА ПЕРИОД:*", f"Сэкономлено/Доступно: {money(available)}"])
    return "\n".join(lines)
