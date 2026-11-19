"""Finite monthly installments saved atomically as dated, grouped expenses."""
import calendar
import secrets
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Category, Family, TransactionGroup, User
from app.repositories import add_transaction, list_categories
from app.rich import esc, link


class InstallmentFlow(StatesGroup):
    amount = State()
    count = State()
    first_date = State()
    category = State()
    comment = State()
    review = State()


def schedule(total, count, first):
    total = Decimal(total)
    if not total.is_finite() or total <= 0 or total != total.quantize(Decimal('.01')):
        raise ValueError('Сумма должна быть положительной, максимум два знака после запятой.')
    if not 2 <= count <= 36:
        raise ValueError('Количество платежей: от 2 до 36.')
    cents = int(total * 100)
    base, remainder = divmod(cents, count)
    if not base:
        raise ValueError('Сумма слишком мала для такого количества платежей.')
    result = []
    for index in range(count):
        year, month = divmod(first.year * 12 + first.month - 1 + index, 12)
        day = min(first.day, calendar.monthrange(year, month + 1)[1])
        result.append((date(year, month + 1, day), Decimal(base + (index < remainder)) / 100))
    return result


def api():
    from app import rich_bot
    return rich_bot


def review_html(data):
    rows = ''.join(f'<tr><td>{i}</td><td>{day:%d.%m.%Y}</td><td>{esc(api().money(amount, data["currency"]))}</td></tr>'
                   for i, (day, amount) in enumerate(schedule(data['total'], data['count'], date.fromisoformat(data['first'])), 1))
    token = data['token']
    return (f'<h3>Покупка в рассрочку</h3><p>Всего: {esc(api().money(Decimal(data["total"]), data["currency"]))}<br/>'
            f'Категория: {esc(data["category_name"])}<br/>Комментарий: {esc(data.get("comment") or "—")}</p>'
            f'<table><tr><th>№</th><th>Дата платежа</th><th>Сумма</th></tr>{rows}</table>'
            '<p>Каждый платёж будет записан датой из расписания и попадёт в свой месяц отчёта. '
            'Округление распределяется по одной агоре; общая сумма не меняется.</p>'
            f'<p>{link(f"inst:{token}:save", "✓ Записать рассрочку")} · {link("inst:restart", "Изменить")} · {link("inst:cancel", "Отмена")}</p>')


async def action(callback, state, bot):
    ui = api()
    user = await ui.ensure_callback_user(callback)
    if not user.family_id or not ui.can_manage_family(user):
        await callback.answer('Недостаточно прав.', show_alert=True)
        return
    parts = callback.data.split(':')
    if parts[-1] == 'cancel':
        await state.clear()
        await ui.rich.edit(bot, callback.message.chat.id, callback.message.message_id, '<p>Рассрочка отменена.</p>')
    elif parts[-1] in {'start', 'restart'}:
        async with SessionLocal() as session:
            family = await session.get(Family, user.family_id)
        await state.clear()
        await state.update_data(token=secrets.token_hex(8), currency=family.currency, family_id=family.id,
                                zone=family.timezone, review_message=None)
        await state.set_state(InstallmentFlow.amount)
        await ui.rich.send(bot, callback.message.chat.id, f'<h3>Покупка в рассрочку</h3><p>Шаг 1 из 5. Введите общую сумму покупки в {esc(family.currency)}.</p><p>{link("inst:cancel", "Отмена")}</p>')
    else:
        data = await state.get_data()
        if data.get('family_id') != user.family_id:
            await callback.answer('Откройте создание рассрочки заново.', show_alert=True)
            return
        if parts[-1] == 'skip' and await state.get_state() == InstallmentFlow.comment.state:
            await state.update_data(comment=None)
            await show_review(bot, callback.message.chat.id, state)
        elif len(parts) == 3 and parts[1] == data.get('token') and parts[2] == 'save' and await state.get_state() == InstallmentFlow.review.state:
            async with SessionLocal() as session, session.begin():
                actor = await session.scalar(select(User).where(User.id == user.id).with_for_update())
                if actor.family_id != data['family_id'] or not ui.can_manage_family(actor):
                    await callback.answer('Недостаточно прав.', show_alert=True)
                    return
                existing = await session.scalar(select(TransactionGroup).where(TransactionGroup.family_id == user.family_id,
                    TransactionGroup.kind == 'installment', TransactionGroup.comment == data['token']))
                if not existing:
                    category = await session.get(Category, data['category_id'])
                    family = await session.get(Family, user.family_id)
                    if not category or category.family_id != user.family_id or category.type != 'expense' or not category.is_active or family.currency != data['currency']:
                        await callback.answer('Категория или валюта изменилась. Создайте рассрочку заново.', show_alert=True)
                        return
                    group = TransactionGroup(family_id=user.family_id, created_by_id=user.id, kind='installment', comment=data['token'])
                    session.add(group)
                    await session.flush()
                    entries = schedule(data['total'], data['count'], date.fromisoformat(data['first']))
                    for index, (day, amount) in enumerate(entries, 1):
                        note = f'Рассрочка {index}/{len(entries)}' + (f': {data["comment"]}' if data.get('comment') else '')
                        tx = await add_transaction(session, user.family_id, user.id, category, amount, 'expense', note)
                        tx.group_id = group.id
                        tx.date = datetime.combine(day, time(12), ZoneInfo(data['zone'])).astimezone(timezone.utc)
            await state.clear()
            await ui.rich.edit(bot, callback.message.chat.id, callback.message.message_id, '<p>✓ Рассрочка записана. Каждый платёж учтён в своём месяце.</p>')
            await ui.send_current_month_report(bot, callback.message.chat.id, user.id, user.family_id)
        else:
            await callback.answer('Это действие устарело. Откройте рассрочку заново.', show_alert=True)
            return
    await callback.answer()


async def show_review(bot, chat_id, state):
    data = await state.get_data()
    await state.set_state(InstallmentFlow.review)
    await api().rich.send(bot, chat_id, review_html(data))


async def value(message, state, bot):
    ui = api()
    user = await ui.ensure_user(message)
    data = await state.get_data()
    if not ui.can_manage_family(user) or data.get('family_id') != user.family_id:
        await state.clear()
        return
    current = await state.get_state()
    raw = (message.text or '').strip()
    try:
        if current == InstallmentFlow.amount.state:
            amount = Decimal(raw.replace(' ', '').replace(',', '.'))
            schedule(amount, 2, date.today())
            await state.update_data(total=str(amount))
            await state.set_state(InstallmentFlow.count)
            prompt = 'Шаг 2 из 5. На сколько ежемесячных платежей разделить сумму? Введите число от 2 до 36.'
        elif current == InstallmentFlow.count.state:
            count = int(raw)
            schedule(data['total'], count, date.today())
            await state.update_data(count=count)
            await state.set_state(InstallmentFlow.first_date)
            prompt = 'Шаг 3 из 5. Введите дату первого платежа: ДД.ММ.ГГГГ. Остальные — ежемесячно в это же число.'
        elif current == InstallmentFlow.first_date.state:
            first = datetime.strptime(raw, '%d.%m.%Y').date()
            schedule(data['total'], data['count'], first)
            await state.update_data(first=first.isoformat())
            await state.set_state(InstallmentFlow.category)
            async with SessionLocal() as session:
                categories = await list_categories(session, user.family_id, 'expense')
            choices = ' · '.join(link(f'instcat:{data["token"]}:{c.id}', c.name) for c in categories)
            prompt = 'Шаг 4 из 5. Выберите категорию.</p><p>' + choices
        else:
            if not raw or len(raw) > 1900:
                raise ValueError('Введите комментарий до 1900 символов или нажмите «Пропустить».')
            await state.update_data(comment=raw)
            await show_review(bot, message.chat.id, state)
            return
    except (ValueError, InvalidOperation, OverflowError):
        await ui.rich.send(bot, message.chat.id, '<p>Проверьте значение: сумма — положительная с точностью до агоры, число платежей — 2–36, дата — ДД.ММ.ГГГГ.</p>')
        return
    await ui.rich.send(bot, message.chat.id, f'<p>{prompt}</p><p>{link("inst:cancel", "Отмена")}</p>')


async def category(callback, state, bot):
    ui = api()
    user = await ui.ensure_callback_user(callback)
    data = await state.get_data()
    _, token, raw_id = callback.data.split(':')
    if token != data.get('token') or await state.get_state() != InstallmentFlow.category.state or not ui.can_manage_family(user) or data.get('family_id') != user.family_id:
        await callback.answer('Выбор устарел.', show_alert=True)
        return
    async with SessionLocal() as session:
        cat = await session.get(Category, int(raw_id)) if raw_id.isdigit() else None
    if not cat or cat.family_id != user.family_id or cat.type != 'expense' or not cat.is_active:
        await callback.answer('Категория недоступна.', show_alert=True)
        return
    await state.update_data(category_id=cat.id, category_name=cat.name)
    await state.set_state(InstallmentFlow.comment)
    await ui.rich.send(bot, callback.message.chat.id, f'<p>Шаг 5 из 5. Введите комментарий к покупке или {link("inst:skip", "Пропустить")}.</p><p>{link("inst:cancel", "Отмена")}</p>')
    await callback.answer()


def register(router):
    router.callback_query.register(action, F.data.startswith('inst:'))
    router.callback_query.register(category, F.data.startswith('instcat:'))
    for stage in (InstallmentFlow.amount, InstallmentFlow.count, InstallmentFlow.first_date, InstallmentFlow.comment):
        router.message.register(value, stage)
