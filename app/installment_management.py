"""Inspect installment groups and atomically change only their remaining rows."""

import hashlib
import json
import secrets
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from app.db import SessionLocal
from app.installments import api, schedule as _schedule
from app.models import Category, Family, Transaction, TransactionGroup, User
from app.repositories import add_transaction, delete_transaction, edit_transaction, list_categories
from app.rich import esc, link


class EditPlan(StatesGroup):
    amount = State()
    count = State()
    first = State()
    comment = State()
    review = State()


def schedule(total, count, first):
    return _schedule(total, count, first, allow_single=True)


def local_day(tx, zone):
    return tx.date.astimezone(ZoneInfo(zone)).date()


def remaining(rows, zone, today=None):
    today = today or datetime.now(ZoneInfo(zone)).date()
    return [tx for tx in rows if tx.deleted_at is None and local_day(tx, zone) >= today]


def fingerprint(rows):
    payload = [
        (
            tx.id,
            str(tx.amount),
            tx.date.isoformat(),
            tx.category_id,
            tx.comment,
            tx.deleted_at.isoformat() if tx.deleted_at else None,
        )
        for tx in rows
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


async def load(session, family_id, group_id, lock=False):
    query = select(TransactionGroup).where(
        TransactionGroup.id == group_id,
        TransactionGroup.family_id == family_id,
        TransactionGroup.kind == "installment",
    )
    group = await session.scalar(query.with_for_update() if lock else query)
    if not group:
        raise ValueError("Рассрочка недоступна.")
    query = (
        select(Transaction)
        .where(Transaction.group_id == group_id, Transaction.family_id == family_id)
        .order_by(Transaction.date, Transaction.id)
    )
    rows = list(await session.scalars(query.with_for_update() if lock else query))
    family = await session.get(Family, family_id)
    return group, rows, family


async def apply_change(session, family_id, group_id, actor_id, change):
    actor = await session.scalar(select(User).where(User.id == actor_id).with_for_update())
    if not actor or actor.family_id != family_id or not api().can_manage_family(actor):
        raise ValueError("Недостаточно прав.")
    group, rows, family = await load(session, family_id, group_id, lock=True)
    if (
        fingerprint(rows) != change["signature"]
        or family.currency != change["currency"]
        or family.timezone != change["zone"]
    ):
        raise ValueError("Расписание или настройки изменились. Откройте карточку заново.")
    pending = remaining(rows, family.timezone)
    if [tx.id for tx in pending] != change["remaining_ids"]:
        raise ValueError("Состав оставшихся платежей изменился. Откройте карточку заново.")
    if not pending:
        raise ValueError("Оставшихся платежей нет.")
    kind = change["kind"]
    if kind == "cancel":
        for tx in pending:
            await delete_transaction(session, tx, actor_id, reason="Отмена остатка рассрочки")
    elif kind == "category":
        cat = await session.get(Category, change["category_id"])
        if not cat or cat.family_id != family_id or cat.type != "expense" or not cat.is_active:
            raise ValueError("Категория недоступна.")
        for tx in pending:
            await edit_transaction(session, tx, actor_id, category_id=cat.id)
    elif kind == "comment":
        for i, tx in enumerate(pending, 1):
            await edit_transaction(
                session,
                tx,
                actor_id,
                comment=f"Рассрочка: остаток {i}/{len(pending)}"
                + (f": {change['comment']}" if change["comment"] else ""),
            )
    elif kind == "schedule":
        entries = schedule(change["total"], change["count"], date.fromisoformat(change["first"]))
        if entries[0][0] < datetime.now(ZoneInfo(family.timezone)).date():
            raise ValueError("Первая дата остатка не может быть в прошлом.")
        category = await session.get(Category, pending[0].category_id)
        if not category or not category.is_active or category.type != "expense":
            raise ValueError("Сначала выберите активную категорию для остатка.")
        note = pending[0].comment or ""
        note = note.split(": ", 1)[-1] if ": " in note else ""
        for i, (day, amount) in enumerate(entries):
            moment = datetime.combine(day, time(12), ZoneInfo(family.timezone)).astimezone(
                timezone.utc
            )
            comment = f"Рассрочка: остаток {i + 1}/{len(entries)}" + (f": {note}" if note else "")
            if i < len(pending):
                await edit_transaction(
                    session, pending[i], actor_id, amount=amount, date=moment, comment=comment
                )
            else:
                tx = await add_transaction(
                    session, family_id, actor_id, category, amount, "expense", comment
                )
                tx.group_id, tx.date = group.id, moment
        for tx in pending[len(entries) :]:
            await delete_transaction(session, tx, actor_id, reason="Пересчёт остатка рассрочки")
    else:
        raise ValueError("Неизвестное действие.")


async def list_html(session, user, page=0):
    groups = list(
        await session.scalars(
            select(TransactionGroup)
            .where(
                TransactionGroup.family_id == user.family_id, TransactionGroup.kind == "installment"
            )
            .order_by(TransactionGroup.id.desc())
            .offset(page * 10)
            .limit(11)
        )
    )
    family = await session.get(Family, user.family_id)
    result = []
    for group in groups[:10]:
        _, rows, _ = await load(session, user.family_id, group.id)
        active = [tx for tx in rows if not tx.deleted_at]
        pending = remaining(rows, family.timezone)
        title = next((tx.comment for tx in active if tx.comment), "Рассрочка")
        if ": " in title:
            title = title.split(": ", 1)[1]
        left = sum((tx.amount for tx in pending), Decimal(0))
        result.append(
            f"<tr><td>{link(f'ip:{group.id}:view:0', f'№{group.id} · {title[:70]}')}</td><td>{len(pending)}</td><td>{esc(api().money(left, family.currency))}</td></tr>"
        )
    pager = []
    if page:
        pager.append(link(f"ip:list:{page - 1}", "← Назад"))
    if len(groups) > 10:
        pager.append(link(f"ip:list:{page + 1}", "Далее →"))
    return (
        "<h3>Рассрочки</h3><table><tr><th>Покупка</th><th>Осталось</th><th>Сумма остатка</th></tr>"
        + ("".join(result) or '<tr><td colspan="3">Рассрочек пока нет.</td></tr>')
        + "</table><p>"
        + " · ".join(pager)
        + f"</p><p>{link('inst:start', '＋ Новая рассрочка') if api().can_manage_family(user) else ''}</p>"
    )


async def detail_html(session, user, group_id, page=0):
    _, rows, family = await load(session, user.family_id, group_id)
    pending = remaining(rows, family.timezone)
    pending_ids = {tx.id for tx in pending}
    labels = {cat.id: cat.name for cat in await list_categories(session, user.family_id)}
    table = []
    for tx in rows[page * 10 : (page + 1) * 10]:
        status = (
            "Отменён" if tx.deleted_at else "Остался" if tx.id in pending_ids else "Дата прошла"
        )
        table.append(
            f"<tr><td>{local_day(tx, family.timezone):%d.%m.%Y}</td><td>{esc(api().money(tx.amount, family.currency))}</td><td>{esc(labels.get(tx.category_id, '—'))}<br/>{esc(tx.comment or '—')}<br/>{status}</td></tr>"
        )
    total = sum((tx.amount for tx in rows if not tx.deleted_at), Decimal(0))
    left = sum((tx.amount for tx in pending), Decimal(0))
    html = f"<h3>Рассрочка №{group_id}</h3><p>Действующее расписание: {esc(api().money(total, family.currency))}<br/>Осталось: {len(pending)} · {esc(api().money(left, family.currency))}</p>"
    html += (
        "<p>Остаток — платежи с сегодняшней даты по часовому поясу семьи. Прошедшая дата не подтверждает списание в банке.</p><table><tr><th>Дата</th><th>Сумма</th><th>Платёж</th></tr>"
        + "".join(table)
        + "</table><p>"
    )
    if page:
        html += link(f"ip:{group_id}:view:{page - 1}", "← Предыдущие") + " · "
    if (page + 1) * 10 < len(rows):
        html += link(f"ip:{group_id}:view:{page + 1}", "Следующие →")
    html += "</p>"
    if pending and api().can_manage_family(user):
        html += (
            "<p>"
            + " · ".join(
                link(f"ip:{group_id}:{kind}", label)
                for kind, label in [
                    ("schedule", "Изменить сумму / число / даты остатка"),
                    ("category", "Категория остатка"),
                    ("comment", "Комментарий остатка"),
                    ("cancel", "Отменить остаток"),
                ]
            )
            + "</p>"
        )
    return html + f"<p>{link('ip:list:0', '← Все рассрочки')}</p>"


async def command(message, bot):
    user = await api().ensure_user(message)
    if not user.family_id:
        return
    async with SessionLocal() as session:
        html = await list_html(session, user)
    await api().rich.send(bot, message.chat.id, html)


async def show_review(bot, chat_id, state):
    data = await state.get_data()
    change = data["plan_change"]
    gid = data["plan_id"]
    token = data["plan_token"]
    text = change["description"]
    if change["kind"] == "schedule":
        text += (
            "<table><tr><th>Дата</th><th>Сумма</th></tr>"
            + "".join(
                f"<tr><td>{day:%d.%m.%Y}</td><td>{esc(api().money(amount, change['currency']))}</td></tr>"
                for day, amount in schedule(
                    change["total"], change["count"], date.fromisoformat(change["first"])
                )
            )
            + "</table>"
        )
    await state.set_state(EditPlan.review)
    await api().rich.send(
        bot,
        chat_id,
        f"<h3>Изменить остаток рассрочки №{gid}</h3>{text}<p>Прошедшие платежи сохранятся.</p><p>{link(f'ip:{gid}:apply:{token}', '✓ Подтвердить')} · {link(f'ip:{gid}:view:0', 'Отмена')}</p>",
    )


async def action(callback, state, bot):
    ui = api()
    user = await ui.ensure_callback_user(callback)
    parts = callback.data.split(":")
    if not user.family_id:
        await callback.answer("Сначала выполните /start.", show_alert=True)
        return
    try:
        if parts[1] == "list":
            page = max(0, int(parts[2]))
            await state.clear()
            async with SessionLocal() as session:
                html = await list_html(session, user, page)
        else:
            gid = int(parts[1])
            kind = parts[2]
            if kind == "view":
                await state.clear()
                async with SessionLocal() as session:
                    html = await detail_html(session, user, gid, max(0, int(parts[3])))
            else:
                if not ui.can_manage_family(user):
                    raise ValueError("Недостаточно прав.")
                data = await state.get_data()
                if kind == "apply":
                    if (
                        await state.get_state() != EditPlan.review.state
                        or data.get("plan_id") != gid
                        or parts[3] != data.get("plan_token")
                    ):
                        raise ValueError("Подтверждение устарело. Откройте карточку заново.")
                    async with SessionLocal() as session, session.begin():
                        await apply_change(
                            session, user.family_id, gid, user.id, data["plan_change"]
                        )
                    await state.clear()
                    async with SessionLocal() as session:
                        html = "<p>✓ Остаток обновлён.</p>" + await detail_html(session, user, gid)
                elif kind == "cat":
                    if (
                        data.get("plan_id") != gid
                        or data.get("plan_change", {}).get("kind") != "category"
                    ):
                        raise ValueError("Выбор устарел.")
                    async with SessionLocal() as session:
                        cat = await session.get(Category, int(parts[3]))
                        if (
                            not cat
                            or cat.family_id != user.family_id
                            or not cat.is_active
                            or cat.type != "expense"
                        ):
                            raise ValueError("Категория недоступна.")
                    change = dict(
                        data["plan_change"],
                        category_id=cat.id,
                        description=f"<p>Новая категория всех оставшихся платежей: {esc(cat.name)}.</p>",
                    )
                    await state.update_data(plan_change=change)
                    await show_review(bot, callback.message.chat.id, state)
                    await callback.answer()
                    return
                else:
                    if kind not in {"cancel", "category", "comment", "schedule"}:
                        raise ValueError("Неизвестное действие.")
                    async with SessionLocal() as session:
                        _, rows, family = await load(session, user.family_id, gid)
                        pending = remaining(rows, family.timezone)
                        if not pending:
                            raise ValueError("Оставшихся платежей нет.")
                        change = dict(
                            kind=kind,
                            signature=fingerprint(rows),
                            remaining_ids=[tx.id for tx in pending],
                            currency=family.currency,
                            zone=family.timezone,
                        )
                        cats = (
                            await list_categories(session, user.family_id, "expense")
                            if kind == "category"
                            else []
                        )
                    await state.clear()
                    await state.update_data(
                        plan_id=gid, plan_token=secrets.token_hex(6), plan_change=change
                    )
                    back = f"<p>{link(f'ip:{gid}:view:0', '← К расписанию')}</p>"
                    if kind == "cancel":
                        change["description"] = (
                            f"<p>Отменить {len(pending)} оставшихся платежей на {esc(ui.money(sum((tx.amount for tx in pending), Decimal(0)), family.currency))}?</p>"
                        )
                        await state.update_data(plan_change=change)
                        await show_review(bot, callback.message.chat.id, state)
                        await callback.answer()
                        return
                    if kind == "category":
                        html = (
                            "<h3>Категория оставшихся платежей</h3><p>"
                            + " · ".join(link(f"ip:{gid}:cat:{cat.id}", cat.name) for cat in cats)
                            + "</p>"
                            + back
                        )
                    elif kind == "comment":
                        await state.set_state(EditPlan.comment)
                        html = (
                            "<p>Введите новый комментарий для всех оставшихся платежей. «-» — убрать комментарий.</p>"
                            + back
                        )
                    else:
                        await state.set_state(EditPlan.amount)
                        html = (
                            f"<p>Введите общую сумму оставшихся платежей в {esc(family.currency)}, а не сумму одного платежа.</p>"
                            + back
                        )
        await ui.rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
        await callback.answer()
    except (ValueError, KeyError, IndexError) as error:
        await callback.answer(str(error), show_alert=True)


async def value(message, state, bot):
    ui = api()
    user = await ui.ensure_user(message)
    data = await state.get_data()
    if not ui.can_manage_family(user) or not data.get("plan_id"):
        await state.clear()
        return
    change = dict(data["plan_change"])
    back = link('ip:{}:view:0'.format(data['plan_id']), '← К расписанию')
    raw = (message.text or "").strip()
    current = await state.get_state()
    try:
        if current == EditPlan.comment.state:
            if not raw or len(raw) > 1900:
                raise ValueError("Введите текст до 1900 символов или «-».")
            change.update(
                comment="" if raw == "-" else raw,
                description=f"<p>Комментарий остатка: {esc(raw)}</p>",
            )
        elif current == EditPlan.amount.state:
            total = Decimal(raw.replace(" ", "").replace(",", "."))
            schedule(total, 1, date.today())
            change["total"] = str(total)
            await state.update_data(plan_change=change)
            await state.set_state(EditPlan.count)
            await ui.rich.send(
                bot,
                message.chat.id,
                f"<p>На сколько платежей разделить остаток? От 1 до 36.</p><p>{back}</p>",
            )
            return
        elif current == EditPlan.count.state:
            change["count"] = int(raw)
            schedule(change["total"], change["count"], date.today())
            await state.update_data(plan_change=change)
            await state.set_state(EditPlan.first)
            await ui.rich.send(
                bot,
                message.chat.id,
                f"<p>Дата первого оставшегося платежа: ДД.ММ.ГГГГ, не раньше сегодняшнего дня.</p><p>{back}</p>",
            )
            return
        else:
            first = datetime.strptime(raw, "%d.%m.%Y").date()
            if first < datetime.now(ZoneInfo(change["zone"])).date():
                raise ValueError("Дата остатка не может быть в прошлом.")
            schedule(change["total"], change["count"], first)
            change.update(
                first=first.isoformat(),
                description=f"<p>Новое расписание остатка: {esc(ui.money(Decimal(change['total']), change['currency']))}.</p>",
            )
        await state.update_data(plan_change=change)
        await show_review(bot, message.chat.id, state)
    except (ValueError, InvalidOperation, OverflowError) as error:
        await ui.rich.send(bot, message.chat.id, f"<p>Проверьте значение. {esc(error)}</p>")


def register(router):
    router.message.register(command, Command("installments"))
    router.callback_query.register(action, F.data.startswith("ip:"))
    for stage in (EditPlan.amount, EditPlan.count, EditPlan.first, EditPlan.comment):
        router.message.register(value, stage)
