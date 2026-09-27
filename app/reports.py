from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Budget, Category, Family, Transaction, User
from app.rich import esc, link

MONTH_NAMES_RU = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def money(value: Decimal | int | float | None, currency: str = "ILS") -> str:
    symbols = {"RUB": "₽", "USD": "$", "EUR": "€", "ILS": "₪", "KZT": "₸"}
    return f"{Decimal(value or 0):,.0f}".replace(",", " ") + " " + symbols.get(currency, currency)


def period_bounds(kind: str, anchor: str | None = None, timezone_name: str = "UTC") -> tuple[datetime, datetime, str, list[str]]:
    now = datetime.now(ZoneInfo(timezone_name))
    try:
        year, month = map(int, (anchor or now.strftime("%Y-%m")).split("-"))
        assert 1 <= month <= 12
    except (ValueError, AssertionError):
        year, month = now.year, now.month
    if kind == "year":
        return datetime(year, 1, 1, tzinfo=now.tzinfo).astimezone(timezone.utc), datetime(year + 1, 1, 1, tzinfo=now.tzinfo).astimezone(timezone.utc), str(year), [f"{year}-{m:02d}" for m in range(1, 13)]
    if kind == "quarter":
        first = ((month - 1) // 3) * 3 + 1
        end_year, end_month = (year + 1, 1) if first == 10 else (year, first + 3)
        return datetime(year, first, 1, tzinfo=now.tzinfo).astimezone(timezone.utc), datetime(end_year, end_month, 1, tzinfo=now.tzinfo).astimezone(timezone.utc), f"Q{((month - 1) // 3) + 1} {year}", [f"{year}-{m:02d}" for m in range(first, first + 3)]
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(year, month, 1, tzinfo=now.tzinfo).astimezone(timezone.utc), datetime(end_year, end_month, 1, tzinfo=now.tzinfo).astimezone(timezone.utc), f"{month:02d}.{year}", [f"{year}-{month:02d}"]


def shift_anchor(anchor: str, kind: str, direction: int) -> str:
    year, month = map(int, anchor.split("-"))
    raw = year * 12 + month - 1 + {"month": 1, "quarter": 3, "year": 12}.get(kind, 1) * direction
    return f"{raw // 12:04d}-{raw % 12 + 1:02d}"


def period_title(kind: str, anchor: str) -> str:
    year, month = map(int, anchor.split("-"))
    if kind == "year":
        return f"{year} год"
    if kind == "quarter":
        return f"{((month - 1) // 3) + 1} квартал {year}"
    return f"{MONTH_NAMES_RU[month - 1].capitalize()} {year}"


async def report_rows(
    session: AsyncSession,
    family_id: int,
    kind: str,
    anchor: str,
    tx_type: str | None = None,
    category_id: int | None = None,
    member_id: int | None = None,
):
    family = await session.get(Family, family_id)
    start, end, label, periods = period_bounds(kind, anchor, family.timezone if family else "UTC")
    conditions = [
        Transaction.family_id == family_id,
        Transaction.deleted_at.is_(None),
        Transaction.date >= start,
        Transaction.date < end,
    ]
    if tx_type in {"income", "expense"}:
        conditions.append(Transaction.type == tx_type)
    if category_id:
        conditions.append(Transaction.category_id == category_id)
    if member_id:
        conditions.append(Transaction.user_id == member_id)
    statement = (
        select(Category.id, Category.name, Transaction.type, func.coalesce(func.sum(Transaction.amount), 0))
        .join(Category, Category.id == Transaction.category_id)
        .where(*conditions)
        .group_by(Category.id, Category.name, Transaction.type)
    )
    facts_data = list(await session.execute(statement))
    # A budget is an effective-from monthly rule, not a value that must be
    # copied into every future month.  For each requested month use the last
    # entered value at or before it.  An edit therefore changes this month and
    # every following month until the next edit, while history stays intact.
    category_rows = list(await session.execute(select(Category.id, Category.name, Category.type)))
    category_meta = {item[0]: (item[1], item[2]) for item in category_rows}
    budget_rows = list(await session.execute(
        select(Budget.category_id, Budget.period, Budget.amount)
        .where(Budget.family_id == family_id, Budget.period <= max(periods))
        .order_by(Budget.period, Budget.category_id)
    ))
    effective: dict[int, Decimal] = {}
    plans: dict[tuple[int, str], Decimal] = {}
    cursor = 0
    for period in periods:
        while cursor < len(budget_rows) and budget_rows[cursor][1] <= period:
            budget_category_id, _, budget_amount = budget_rows[cursor]
            effective[budget_category_id] = Decimal(budget_amount)
            cursor += 1
        for budget_category_id, amount in effective.items():
            name, category_type = category_meta.get(budget_category_id, (None, None))
            if not name or (tx_type and category_type != tx_type) or (category_id and budget_category_id != category_id):
                continue
            key = (budget_category_id, category_type)
            plans[key] = plans.get(key, Decimal("0")) + amount
    facts = {(item[0], item[2]): Decimal(item[3]) for item in facts_data}
    names = {(item[0], item[2]): item[1] for item in facts_data}
    names.update({(category_id, category_type): name for category_id, (name, category_type) in category_meta.items() if (category_id, category_type) in plans})
    rows = [{"id": category_id, "name": names[key], "type": tx_type, "plan": plans.get(key, Decimal(0)), "fact": facts.get(key, Decimal(0))} for key in set(plans) | set(facts) for category_id, tx_type in [key]]
    for row in rows:
        row["delta"] = row["plan"] - row["fact"]
    income_plan = sum(row["plan"] for row in rows if row["type"] == "income")
    income_fact = sum(row["fact"] for row in rows if row["type"] == "income")
    expense_fact = sum(row["fact"] for row in rows if row["type"] == "expense")
    return label, rows, income_plan, income_fact, expense_fact


async def category_detail(
    session: AsyncSession, family_id: int, category_id: int, kind: str, anchor: str, page: int = 0
) -> str:
    family = await session.get(Family, family_id)
    start, end, label, _ = period_bounds(kind, anchor, family.timezone if family else "UTC")
    category = await session.get(Category, category_id)
    if not category or category.family_id != family_id:
        return "<p>Категория недоступна.</p>"
    page = max(page, 0)
    items = await session.execute(
        select(Transaction.id, Transaction.date, Transaction.amount, Transaction.comment)
        .where(Transaction.family_id == family_id, Transaction.deleted_at.is_(None), Transaction.category_id == category_id, Transaction.date >= start, Transaction.date < end)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .offset(page * 25)
        .limit(25)
    )
    timezone_name = family.timezone if family else "UTC"
    currency = family.currency if family else "ILS"
    zone = ZoneInfo(timezone_name)
    body = "".join(
        f"<tr><td>{link(f'txopen:{transaction_id}', date.astimezone(zone).strftime('%d.%m %H:%M'))}</td>"
        f"<td>{link(f'txopen:{transaction_id}', money(amount, currency))}</td><td>{esc(comment or '—')}</td></tr>"
        for transaction_id, date, amount, comment in items
    ) or '<tr><td colspan="3">Операций за период нет.</td></tr>'
    return f"<h3>{esc(category.name)} · {esc(label)}</h3><table><tr><th>Дата</th><th>Сумма</th><th>Комментарий</th></tr>{body}</table>"


async def build_report_html(
    session: AsyncSession,
    family_id: int,
    state: dict[str, str],
    token: str,
    viewer_id: int | None = None,
) -> str:
    kind, anchor = state.get("kind", "month"), state.get("anchor", datetime.now(timezone.utc).strftime("%Y-%m"))
    family = await session.get(Family, family_id)
    currency = family.currency if family else "ILS"
    label, rows, income_plan, income_fact, expense_fact = await report_rows(
        session, family_id, kind, anchor
    )
    sort, descending = state.get("sort", "fact"), state.get("descending", "1") == "1"

    def header(field: str, title: str) -> str:
        marker = " ↓" if sort == field and descending else " ↑" if sort == field else " ↕"
        return link(f"r:{token}:s:{field}", title + marker)

    compare_anchor = shift_anchor(anchor, kind, -1)
    _, prior_rows, _, prior_income, prior_expense = await report_rows(session, family_id, kind, compare_anchor)
    prior_facts = {(row["id"], row["type"]): Decimal(row["fact"]) for row in prior_rows}

    def period_change(value: Decimal, prior: Decimal) -> str:
        delta = value - prior
        sign = "+" if delta > 0 else ""
        if prior == 0:
            suffix = "новые" if value else "0%"
        else:
            percent = (delta * Decimal("100") / prior).quantize(Decimal("0.1"))
            suffix = f"{'+' if percent > 0 else ''}{percent}%"
        return f"{sign}{money(delta, currency)} ({suffix})"

    def row_key(row: dict) -> object:
        return str(row["name"]).lower() if sort == "name" else row[sort]

    open_type = state.get("open_type")
    open_category = state.get("open_category")
    open_category_id = int(open_category) if open_category and open_category.isdigit() else None
    start, end, _, _ = period_bounds(kind, anchor, family.timezone if family else "UTC")
    transactions_by_category: dict[int, list[Transaction]] = {}
    if open_category_id:
        transactions = list(await session.scalars(
            select(Transaction)
            .where(
                Transaction.family_id == family_id, Transaction.deleted_at.is_(None),
                Transaction.category_id == open_category_id, Transaction.date >= start, Transaction.date < end,
            )
        ))
        transactions_by_category[open_category_id] = transactions
    viewer = await session.get(User, viewer_id) if viewer_id else None
    can_edit = bool(viewer and viewer.family_id == family_id and viewer.role in {"owner", "editor"})
    zone = ZoneInfo(family.timezone if family else "UTC")
    table_parts: list[str] = []
    for tx_type, title, plan_total, fact_total in (
        ("income", "Доходы", income_plan, income_fact),
        ("expense", "Расходы", sum((row["plan"] for row in rows if row["type"] == "expense"), Decimal("0")), expense_fact),
    ):
        delta_total = plan_total - fact_total
        type_link = link(f"r:{token}:e:{tx_type}", ("▾ " if open_type == tx_type else "▸ ") + title)
        prior_total = prior_income if tx_type == "income" else prior_expense
        table_parts.append(f"<tr><td colspan=\"3\"><b>{type_link}</b></td><td>{esc(money(plan_total, currency))}</td><td>{esc(money(fact_total, currency))}</td><td>{esc(money(delta_total, currency))}</td><td>{esc(period_change(fact_total, prior_total))}</td><td></td></tr>")
        if open_type != tx_type:
            continue
        categories = sorted((row for row in rows if row["type"] == tx_type), key=row_key, reverse=descending)
        for row in categories:
            category_id = int(row["id"])
            expanded = open_category_id == category_id
            category_link = link(
                f"r:{token}:c:{category_id}",
                ("▾ " if expanded else "▸ ") + str(row["name"]),
            )
            prior_fact = prior_facts.get((category_id, tx_type), Decimal("0"))
            table_parts.append(f"<tr><td>&nbsp;</td><td colspan=\"2\">{category_link}</td><td>{esc(money(row['plan'], currency))}</td><td>{esc(money(row['fact'], currency))}</td><td>{esc(money(row['delta'], currency))}</td><td>{esc(period_change(Decimal(row['fact']), prior_fact))}</td><td></td></tr>")
            if not expanded:
                continue
            operations = transactions_by_category.get(category_id, [])
            operations.sort(key=lambda tx: (str(tx.comment or "").lower() if sort == "name" else (tx.amount if sort in {"fact", "plan", "delta"} else tx.date)), reverse=descending)
            for tx in operations:
                operation_name = tx.date.astimezone(zone).strftime("%d.%m %H:%M")
                tools = ""
                if can_edit:
                    tools = link(f"txe:{tx.id}:amount", "✎") + " " + link(f"txe:{tx.id}:delete", "🗑")
                table_parts.append(f"<tr><td>&nbsp;</td><td>&nbsp;</td><td>{link(f'txopen:{tx.id}', operation_name)}</td><td>—</td><td>{esc(money(tx.amount, currency))}</td><td>—</td><td>—</td><td>{tools}</td></tr>")

    current_anchor = datetime.now(ZoneInfo(family.timezone if family else "UTC")).strftime("%Y-%m")
    next_link = link(f"r:{token}:p:next", "→") if shift_anchor(anchor, kind, 1) <= current_anchor else ""
    period_nav = f"{link(f'r:{token}:p:prev', '←')} {link(f'r:{token}:v', period_title(kind, anchor))} {next_link}"
    return f'<h3>📊 Бюджет · {period_nav} · {link(f"r:{token}:x", "↻")}</h3><table><tr><th colspan="3">{header("name", "Статья / операция")}</th><th>{header("plan", "План")}</th><th>{header("fact", "Факт")}</th><th>{header("delta", "Остаток")}</th><th>К прошлому</th><th></th></tr>{"".join(table_parts)}</table><p><b>Доступно:</b> {esc(money(income_fact - expense_fact, currency))}</p>'
