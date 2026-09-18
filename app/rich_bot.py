from __future__ import annotations

# The handlers intentionally use compact one-line branches where a callback
# must answer before returning. Formatting is otherwise enforced project-wide.
# ruff: noqa: E701, E702

import logging
import secrets
import mimetypes
import re
import asyncio
import base64
import json
from dataclasses import dataclass
from contextlib import suppress
from collections import OrderedDict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, Message
from openai import AsyncOpenAI
from sqlalchemy import select

from app.ai import AIParser, AIUnavailableError, ParsedTransaction, reconcile_receipt_totals
from app.config import settings
from app.db import SessionLocal
from app.models import Account, Category, Family, FamilyInvite, InteractiveScreen, RecurringTransaction, Transaction, TransactionDraft, Transfer, User
from app.reports import build_report_html, money, report_rows, shift_anchor
from app.exchange import convert_amount
from app.receipts import ENABLE_RECEIPT_URLS, ReceiptError, extract_pdf_text, fetch_receipt_url, is_http_url, render_pdf_pages
from app.repositories import (
    add_transaction, copy_budgets_from_period, create_account, create_category, create_family_for_user,
    create_recurring_transaction, create_split_payment, create_transfer,
    create_family_invite, delete_transaction, edit_transaction, find_category,
    get_or_create_user, join_family, list_categories, rotate_family_invite,
    materialize_recurring_transaction, set_receipt_retention_policy, update_family_preferences, upsert_budget,
)
from app.rich import RichMessageService, esc, link
from app.settings_store import (
    EDITABLE_KEYS,
    HELP as SETTINGS_HELP,
    LABELS as SETTINGS_LABELS,
    display_value,
    get_family_ai_settings,
    get_values,
    set_family_openai_api_key,
    set_family_openai_model,
    set_value,
)

router = Router()
ai_parser = AIParser()
rich = RichMessageService()


@dataclass
class ReceiptAlbum:
    user_id: int
    chat_id: int
    messages: list[Message]
    expires_at: datetime


# Telegram sends album members as independent updates.  The short-lived buffer
# turns them into one deliberate choice before any image is sent to OpenAI.
_pending_receipt_albums: dict[str, list[Message]] = {}
_receipt_album_choices: dict[str, ReceiptAlbum] = {}


class InviteOnlyMiddleware(BaseMiddleware):
    """Reject strangers before any handler can create a user record."""

    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        if not actor or is_owner(actor.id):
            return await handler(event, data)
        text = getattr(event, "text", "") or ""
        if isinstance(event, Message) and text.partition(" ")[2].startswith("join_") and text.startswith("/start"):
            return await handler(event, data)
        async with SessionLocal() as session:
            user = await session.get(User, actor.id)
        if user and user.family_id:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ возможен только по приглашению семьи.", show_alert=True)
        elif isinstance(event, Message):
            await rich.send(event.bot, event.chat.id, "<p>Доступ возможен только по полной ссылке приглашения от владельца семьи.</p>")
        return None


router.message.outer_middleware(InviteOnlyMiddleware())
router.callback_query.outer_middleware(InviteOnlyMiddleware())


class JoinFamily(StatesGroup):
    waiting_code = State()


class BudgetFlow(StatesGroup):
    waiting_amount = State()


class SettingsFlow(StatesGroup):
    waiting_value = State()


class TransactionEditFlow(StatesGroup):
    waiting_value = State()


class FamilySettingsFlow(StatesGroup):
    waiting_value = State()


class CategoryFlow(StatesGroup):
    waiting_name = State()


class AdvancedFlow(StatesGroup):
    waiting_value = State()


class RecurringFlow(StatesGroup):
    waiting_amount = State()
    waiting_first_run = State()
    waiting_comment = State()


class RecurringEditFlow(StatesGroup):
    waiting_value = State()


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands([BotCommand(command=name, description=description) for name, description in (("start", "Инициализация"), ("budget", "Лимиты"), ("report", "Отчет"), ("transactions", "Операции"), ("categories", "Категории"), ("recurring", "Регулярные операции"), ("split", "Разбить платёж"), ("family", "Семья и приглашения"), ("settings", "Настройки приложения"), ("help", "Помощь"))])


async def ensure_user(message: Message) -> User:
    async with SessionLocal() as session, session.begin():
        return await get_or_create_user(session, message.from_user.id, message.from_user.username)


async def ensure_callback_user(callback: CallbackQuery) -> User:
    """Use the person who pressed a rich link, never the bot-authored message."""
    async with SessionLocal() as session, session.begin():
        return await get_or_create_user(session, callback.from_user.id, callback.from_user.username)


def is_owner(telegram_id: int) -> bool:
    return settings.owner_telegram_id is not None and telegram_id == settings.owner_telegram_id


def can_manage_family(user: User | None) -> bool:
    return bool(user and user.family_id and user.role in {"owner", "editor"})


def invitation_url(bot_username: str, code: str) -> str:
    """A Telegram deep link: opening it delivers `/start join_<code>` to us."""
    return f"https://t.me/{bot_username.lstrip('@')}?start=join_{code}"


def invitation_anchor(url: str) -> str:
    return f'<a href="{esc(url)}">{esc(url)}</a>'


async def render_settings(bot: Bot, chat_id: int) -> None:
    async with SessionLocal() as session:
        values = await get_values(session)
    rows = "".join(
        "<tr>"
        f"<td>{esc(SETTINGS_LABELS[key])}</td>"
        f"<td><code>{esc(display_value(key, values[key]))}</code></td>"
        f"<td>{link(f'settings:{key}', 'Изменить')}</td>"
        "</tr>"
        for key in EDITABLE_KEYS
    )
    await rich.send(
        bot,
        chat_id,
        "<h3>Глобальные настройки приложения</h3>"
        "<p>Доступны только администратору из .env. Ключ OpenAI и модель задаются владельцем в разделе /family.</p>"
        f"<table><tr><th>Параметр</th><th>Значение</th><th></th></tr>{rows}</table>",
    )


async def render_family_model_choices(bot: Bot, chat_id: int, family_id: int, page: int = 0) -> None:
    """Show only model IDs supplied by this family's OpenAI account."""
    async with SessionLocal() as session:
        runtime = await get_family_ai_settings(session, family_id)
    if not runtime.openai_api_key:
        await rich.send(
            bot,
            chat_id,
            "<p>Сначала задайте <b>OpenAI API key</b> в настройках семьи. После этого бот загрузит доступные у провайдера модели.</p>",
        )
        return
    client = AsyncOpenAI(api_key=runtime.openai_api_key)
    try:
        response = await client.models.list()
    except Exception as error:
        logging.warning("Could not list OpenAI models: %s", error)
        await rich.send(bot, chat_id, "<p>Не удалось получить модели у провайдера. Проверьте API-ключ и доступ к OpenAI.</p>")
        return
    finally:
        await client.close()

    # The bot uses image input for receipts via Chat Completions. GPT models are
    # the compatible family; the list itself is never invented or hard-coded.
    model_ids = sorted(
        {
            model.id
            for model in response.data
            if model.id.startswith("gpt-") and len(f"family:model:{model.id}") <= 64
        },
        reverse=True,
    )
    if not model_ids:
        await rich.send(bot, chat_id, "<p>Провайдер не вернул совместимых GPT-моделей для обработки чеков.</p>")
        return
    page_size = 20
    page_count = max(1, (len(model_ids) + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    page_models = model_ids[page * page_size : (page + 1) * page_size]
    rows = "".join(
        f"<tr><td><code>{esc(model_id)}</code></td><td>{link(f'family:model:{model_id}', 'Выбрать')}</td></tr>"
        for model_id in page_models
    )
    pager = ""
    if page_count > 1:
        links = []
        if page:
            links.append(link(f"family:model_page:{page - 1}", "← Предыдущие"))
        links.append(f"{page + 1}/{page_count}")
        if page + 1 < page_count:
            links.append(link(f"family:model_page:{page + 1}", "Следующие →"))
        pager = f"<p>{' · '.join(links)}</p>"
    await rich.send(
        bot,
        chat_id,
        "<h3>Модель OpenAI семьи</h3><p>Выберите модель, которую вернул аккаунт этой семьи. Для чеков нужна модель с поддержкой изображений.</p>"
        f"<table><tr><th>Модель</th><th></th></tr>{rows}</table>{pager}",
    )


def expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=24)


async def create_screen(user: User, chat_id: int, kind: str, state: dict[str, str]) -> InteractiveScreen:
    screen = InteractiveScreen(token=secrets.token_urlsafe(12), kind=kind, user_id=user.id, family_id=user.family_id, chat_id=chat_id, state=state, expires_at=expiry())
    async with SessionLocal() as session, session.begin():
        session.add(screen)
        await session.flush()
        return screen


async def create_report_screen(user: User, chat_id: int, restore_view: bool) -> InteractiveScreen:
    """Open a report with the user's last table view when requested from UI."""
    async with SessionLocal() as session:
        family = await session.get(Family, user.family_id)
        now_anchor = datetime.now(ZoneInfo(family.timezone if family else "UTC")).strftime("%Y-%m")
        state: dict[str, str] = {"kind": "month", "anchor": now_anchor, "sort": "fact", "descending": "1"}
        if restore_view:
            previous = await session.scalar(
                select(InteractiveScreen)
                .where(InteractiveScreen.kind == "report", InteractiveScreen.user_id == user.id, InteractiveScreen.family_id == user.family_id)
                .order_by(InteractiveScreen.id.desc())
            )
            if previous:
                allowed = {"kind", "anchor", "sort", "descending", "open_type", "open_category"}
                state.update({key: value for key, value in previous.state.items() if key in allowed and isinstance(value, str)})
    return await create_screen(user, chat_id, "report", state)


async def send_screen(bot: Bot, screen: InteractiveScreen, html: str) -> None:
    result = await rich.send(bot, screen.chat_id, html)
    async with SessionLocal() as session, session.begin():
        stored = await session.get(InteractiveScreen, screen.id)
        stored.message_id = result.get("message_id")


async def notify_other_family_members(
    bot: Bot,
    family_id: int,
    actor_id: int,
    entries: list[tuple[Decimal, str, str, str | None]],
) -> None:
    """Notify all family members except the author, without risking the save."""
    async with SessionLocal() as session:
        family = await session.get(Family, family_id)
        if not family or not family.notify_members_on_transactions:
            return
        actor = await session.get(User, actor_id)
        recipients = list(await session.scalars(select(User).where(User.family_id == family_id, User.id != actor_id)))
    if not recipients:
        return
    actor_name = esc((actor.username if actor else None) or str(actor_id))
    rows = "".join(
        f"<tr><td>{'Доход' if kind == 'income' else 'Расход'}</td><td>{esc(money(amount, family.currency))}</td><td>{esc(category)}</td><td>{esc(comment or '—')}</td></tr>"
        for amount, kind, category, comment in entries
    )
    html = f"<h3>Новая операция от {actor_name}</h3><table><tr><th>Тип</th><th>Сумма</th><th>Категория</th><th>Комментарий</th></tr>{rows}</table>"
    for recipient in recipients:
        try:
            await rich.send(bot, recipient.id, html)
        except Exception:
            logging.getLogger(__name__).warning("Could not notify family member %s", recipient.id, exc_info=True)


async def render_report(bot: Bot, screen: InteractiveScreen, edit: bool = False) -> None:
    async with SessionLocal() as session:
        html = await build_report_html(
            session,
            screen.family_id,
            screen.state,
            screen.token,
            viewer_id=screen.user_id,
        )
    if edit and screen.message_id:
        await rich.edit(bot, screen.chat_id, screen.message_id, html)
    else:
        result = await rich.send(bot, screen.chat_id, html)
        async with SessionLocal() as session, session.begin():
            stored = await session.get(InteractiveScreen, screen.id)
            stored.message_id = result.get("message_id")


async def send_current_month_report(bot: Bot, chat_id: int, actor_id: int, family_id: int) -> None:
    """Show the actor the live monthly table immediately after a save."""
    async with SessionLocal() as session:
        actor = await session.get(User, actor_id)
        family = await session.get(Family, family_id)
    if not actor or not family:
        return
    screen = await create_report_screen(actor, chat_id, restore_view=False)
    await render_report(bot, screen)


async def report_filter_html(session, screen: InteractiveScreen, filter_kind: str) -> str:
    """Render a server-bound filter chooser; all controls remain rich links."""
    token, family_id = screen.token, screen.family_id
    back = link(f"r:{token}:b", "← К отчёту")
    if filter_kind == "type":
        choices = [("all", "Все типы"), ("expense", "Расходы"), ("income", "Доходы")]
        return "<h3>Тип операций</h3><p>" + " · ".join(link(f"r:{token}:ft:{key}", name) for key, name in choices) + f"</p><p>{back}</p>"
    if filter_kind == "category":
        categories = await list_categories(session, family_id)
        choices = [link(f"r:{token}:fc:all", "Все категории")] + [
            link(f"r:{token}:fc:{item.id}", item.name) for item in categories
        ]
        return "<h3>Категория</h3><p>" + " · ".join(choices) + f"</p><p>{back}</p>"
    members = list(await session.scalars(select(User).where(User.family_id == family_id).order_by(User.username, User.id)))
    choices = [link(f"r:{token}:fm:all", "Все участники")] + [
        link(f"r:{token}:fm:{item.id}", item.username or str(item.id)) for item in members
    ]
    return "<h3>Участник</h3><p>" + " · ".join(choices) + f"</p><p>{back}</p>"


async def report_period_picker_html(session, screen: InteractiveScreen) -> str:
    """Compact in-message calendar; all controls are rich inline links."""
    family = await session.get(Family, screen.family_id)
    kind = screen.state.get("kind", "month")
    anchor = screen.state.get("anchor", datetime.now(ZoneInfo(family.timezone if family else "UTC")).strftime("%Y-%m"))
    year = int(screen.state.get("picker_year", anchor[:4]))
    now = datetime.now(ZoneInfo(family.timezone if family else "UTC"))
    current = now.strftime("%Y-%m")
    token = screen.token
    modes = " · ".join(
        title if item == kind else link(f"r:{token}:pk:{item}", title)
        for item, title in (("month", "Месяц"), ("quarter", "Квартал"), ("year", "Год"))
    )
    year_nav = f"{link(f'r:{token}:py:prev', '‹')} {year} {link(f'r:{token}:py:next', '›')}"
    if kind == "month":
        choices = []
        for month, title in enumerate(("Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"), 1):
            candidate = f"{year}-{month:02d}"
            choices.append(title if candidate > current else link(f"r:{token}:ps:{candidate}", ("✓ " if candidate == anchor else "") + title))
        body = " · ".join(choices)
    elif kind == "quarter":
        choices = []
        for month, title in ((1, "I квартал"), (4, "II квартал"), (7, "III квартал"), (10, "IV квартал")):
            candidate = f"{year}-{month:02d}"
            choices.append(title if candidate > current else link(f"r:{token}:ps:{candidate}", ("✓ " if ((int(anchor[5:7]) - 1) // 3) == ((month - 1) // 3) and anchor[:4] == str(year) else "") + title))
        body = " · ".join(choices)
    else:
        choices = []
        for candidate_year in range(year - 2, year + 3):
            candidate = f"{candidate_year}-01"
            choices.append(str(candidate_year) if candidate > current else link(f"r:{token}:ps:{candidate}", ("✓ " if anchor[:4] == str(candidate_year) else "") + str(candidate_year)))
        body = " · ".join(choices)
    current_link = "" if anchor == current else f"<p>{link(f'r:{token}:pt', 'К текущему периоду')}</p>"
    return f"<h3>Выбор периода</h3><p>{modes}</p><p>{year_nav}</p><p>{body}</p>{current_link}<p>{link(f'r:{token}:b', '← К отчёту')}</p>"


@router.message(Command("start"))
async def start(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    payload = (message.text or "").partition(" ")[2].strip()
    if payload.startswith("join_"):
        code = payload.removeprefix("join_")
        if not code or len(code) > 48:
            await rich.send(bot, message.chat.id, "<p>Ссылка приглашения некорректна.</p>")
            return
        async with SessionLocal() as session, session.begin():
            user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
            family = await join_family(session, user, code)
        if family:
            await rich.send(bot, message.chat.id, f"<h3>Вы подключены</h3><p>Семейный бюджет: <b>{esc(family.name)}</b>.</p><p>Теперь можно ввести расход, например <code>500 Продукты</code>.</p>")
        else:
            await rich.send(bot, message.chat.id, "<p>Эта ссылка приглашения недействительна или была отозвана. Попросите владельца создать новую.</p>")
        return
    if user.family_id:
        settings_link = f" · {link('ui:settings', 'Настройки')}" if is_owner(message.from_user.id) else ""
        await rich.send(bot, message.chat.id, "<h3>Семейный бюджет</h3><p>Введите, например, <code>500 Продукты молоко</code>, или откройте " + link("ui:report", "отчет") + settings_link + ".</p>")
        return
    await rich.send(bot, message.chat.id, "<h3>Семейный бюджет</h3><p>" + link("family:create", "Создать семью") + " · " + link("family:join", "Ввести код приглашения") + "</p>")


@router.message(Command("help"))
async def help_command(message: Message, bot: Bot) -> None:
    await rich.send(bot, message.chat.id, "<h3>Как записывать операции</h3><p><code>500 Продукты молоко</code> — расход.<br/><code>+150000 Зарплата</code> — доход.</p>")


@router.message(Command("settings"))
async def settings_command(message: Message, bot: Bot) -> None:
    await ensure_user(message)
    if not is_owner(message.from_user.id):
        await rich.send(bot, message.chat.id, "<p>Настройки приложения доступны только владельцу.</p>")
        return
    await render_settings(bot, message.chat.id)


@router.callback_query(F.data == "ui:settings")
async def settings_shortcut(callback: CallbackQuery, bot: Bot) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Настройки доступны только владельцу.", show_alert=True)
        return
    await render_settings(bot, callback.message.chat.id)
    await callback.answer()


@router.callback_query(F.data.startswith("settings:"))
async def settings_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    key = callback.data.split(":", 1)[1]
    if not is_owner(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if key == "cancel":
        await state.clear()
        await rich.send(bot, callback.message.chat.id, "<p>Изменение отменено.</p>")
        await callback.answer()
        return
    if key not in EDITABLE_KEYS:
        await callback.answer("Неизвестная настройка.", show_alert=True)
        return
    await state.update_data(settings_key=key)
    await state.set_state(SettingsFlow.waiting_value)
    await rich.send(
        bot,
        callback.message.chat.id,
        f"<h3>{esc(SETTINGS_LABELS[key])}</h3><p>{esc(SETTINGS_HELP[key])}</p>"
        f"<p>{link('settings:cancel', 'Отмена')}</p>",
    )
    await callback.answer()


@router.message(SettingsFlow.waiting_value)
async def settings_value(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_owner(message.from_user.id):
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Настройки приложения доступны только владельцу.</p>")
        return
    data = await state.get_data()
    key = data.get("settings_key")
    if key not in EDITABLE_KEYS:
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Сессия настройки устарела. Откройте /settings заново.</p>")
        return
    try:
        async with SessionLocal() as session, session.begin():
            await set_value(session, key, message.text or "")
    except ValueError as error:
        await rich.send(bot, message.chat.id, f"<p>{esc(error)}</p><p>{esc(SETTINGS_HELP[key])}</p>")
        return
    await state.clear()
    if key == "log_level":
        logging.getLogger().setLevel((message.text or "").strip().upper())
    await rich.send(bot, message.chat.id, "<p>Настройка сохранена.</p>")
    await render_settings(bot, message.chat.id)


@router.message(Command("categories"))
async def categories(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>")
        return
    async with SessionLocal() as session:
        items = await list_categories(session, user.family_id, active_only=False)
    rows = "".join(
        "<tr>"
        f"<td>{esc(item.name)}</td><td>{'Доход' if item.type == 'income' else 'Расход'}</td>"
        f"<td>{'Активна' if item.is_active else 'В архиве'}</td>"
        f"<td>{link(f'cat:{item.id}:archive', 'Архивировать') if item.is_active and can_manage_family(user) else '—'}</td>"
        "</tr>"
        for item in items
    ) or '<tr><td colspan="4">Категорий пока нет.</td></tr>'
    actions = (
        f"<p>{link('cat:new:expense', '＋ Расход')} · {link('cat:new:income', '＋ Доход')}</p>"
        if can_manage_family(user)
        else ""
    )
    await rich.send(
        bot,
        message.chat.id,
        f"<h3>Категории</h3><table><tr><th>Название</th><th>Тип</th><th>Статус</th><th></th></tr>{rows}</table>{actions}",
    )


@router.callback_query(F.data.startswith("cat:"))
async def category_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = callback.data.split(":")
    async with SessionLocal() as session, session.begin():
        user = await session.get(User, callback.from_user.id)
        if not can_manage_family(user):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if len(parts) == 3 and parts[1] == "new" and parts[2] in {"income", "expense"}:
            await state.update_data(category_type=parts[2])
            await state.set_state(CategoryFlow.waiting_name)
            await rich.send(bot, callback.message.chat.id, "<p>Введите название новой категории.</p>")
            await callback.answer()
            return
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] != "archive":
            await callback.answer("Некорректное действие.", show_alert=True)
            return
        category = await session.get(Category, int(parts[1]))
        if not category or category.family_id != user.family_id:
            await callback.answer("Категория недоступна.", show_alert=True)
            return
        category.is_active = False
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, "<p>Категория архивирована. История операций сохранена.</p>")
    await callback.answer()


@router.message(CategoryFlow.waiting_name)
async def category_name(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    name, tx_type = (message.text or "").strip(), data.get("category_type")
    if not name or len(name) > 120 or tx_type not in {"income", "expense"}:
        await rich.send(bot, message.chat.id, "<p>Введите название до 120 символов.</p>")
        return
    user = await ensure_user(message)
    if not can_manage_family(user):
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Недостаточно прав.</p>")
        return
    async with SessionLocal() as session, session.begin():
        exists = await find_category(session, user.family_id, name, tx_type)
        if exists:
            if not exists.is_active:
                exists.is_active = True
            result = "Категория уже существовала; она активна."
        else:
            await create_category(session, user.family_id, name, tx_type)
            result = "Категория создана."
    await state.clear()
    await rich.send(bot, message.chat.id, f"<p>{result}</p>")


@router.message(Command("report"))
async def report(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>")
        return
    screen = await create_report_screen(user, message.chat.id, restore_view=True)
    await render_report(bot, screen)


async def advanced_start(callback: CallbackQuery, state: FSMContext, bot: Bot, mode: str, prompt: str) -> None:
    user = await ensure_callback_user(callback)
    if not can_manage_family(user):
        await callback.answer("Роль viewer может только просматривать данные.", show_alert=True)
        return
    await state.update_data(advanced_mode=mode)
    await state.set_state(AdvancedFlow.waiting_value)
    await rich.send(bot, callback.message.chat.id, f"<p>{prompt}</p>")
    await callback.answer()


@router.message(Command("accounts"))
async def accounts(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>"); return
    async with SessionLocal() as session:
        items = list(await session.scalars(select(Account).where(Account.family_id == user.family_id).order_by(Account.name)))
    rows = "".join(f"<tr><td>{esc(item.name)}</td><td>{esc(item.currency)}</td><td>{'Активен' if item.is_active else 'Архив'}</td></tr>" for item in items) or '<tr><td colspan="3">Счетов пока нет.</td></tr>'
    action = f"<p>{link('adv:account', '＋ Добавить счёт')}</p>" if can_manage_family(user) else ""
    await rich.send(bot, message.chat.id, f"<h3>Счета</h3><table><tr><th>Название</th><th>Валюта</th><th>Статус</th></tr>{rows}</table>{action}")


@router.message(Command("transfer"))
async def transfer_command(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>"); return
    async with SessionLocal() as session:
        items = list(await session.scalars(select(Transfer).where(Transfer.family_id == user.family_id).order_by(Transfer.date.desc()).limit(20)))
    rows = "".join(f"<tr><td>{esc(item.date.strftime('%d.%m %H:%M'))}</td><td>{esc(money(item.amount))}</td></tr>" for item in items) or '<tr><td colspan="2">Переводов пока нет.</td></tr>'
    action = f"<p>{link('adv:transfer', '＋ Новый перевод')}</p>" if can_manage_family(user) else ""
    await rich.send(bot, message.chat.id, f"<h3>Переводы</h3><table><tr><th>Дата</th><th>Сумма</th></tr>{rows}</table>{action}")


@router.message(Command("recurring"))
async def recurring_command(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>"); return
    async with SessionLocal() as session:
        items = list(await session.scalars(select(RecurringTransaction).where(RecurringTransaction.family_id == user.family_id).order_by(RecurringTransaction.next_run_at).limit(30)))
    rows = "".join(
        f"<tr><td>{link(f'rcopen:{item.id}', 'Неделя' if item.cadence == 'weekly' else 'Месяц')}</td>"
        f"<td>{link(f'rcopen:{item.id}', money(item.amount))}</td><td>{esc(item.next_run_at.strftime('%d.%m.%Y'))}</td>"
        f"<td>{'Активно' if item.is_active else 'Остановлено'}</td><td>{link(f'rce:{item.id}:delete', '🗑') if can_manage_family(user) else ''}</td></tr>"
        for item in items
    ) or '<tr><td colspan="5">Правил пока нет.</td></tr>'
    action = f"<p>{link('adv:recurring', '＋ Новая регулярная операция')}</p>" if can_manage_family(user) else ""
    await rich.send(bot, message.chat.id, f"<h3>Регулярные операции</h3><table><tr><th>Период</th><th>Сумма</th><th>Следующая</th><th>Статус</th><th></th></tr>{rows}</table>{action}")


@router.message(Command("split"))
async def split_command(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>"); return
    action = f"<p>{link('adv:split', '＋ Разбить расход')}</p>" if can_manage_family(user) else "<p>Роль viewer может только просматривать данные.</p>"
    await rich.send(bot, message.chat.id, "<h3>Разбить платёж</h3><p>Создаёт несколько связанных операций с разными категориями.</p>" + action)


@router.callback_query(F.data.startswith("rcopen:"))
async def recurring_detail(callback: CallbackQuery, bot: Bot) -> None:
    raw_id = callback.data.split(":", 1)[1]
    if not raw_id.isdigit():
        await callback.answer("Некорректное правило", show_alert=True); return
    async with SessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        rule = await session.get(RecurringTransaction, int(raw_id))
        category = await session.get(Category, rule.category_id) if rule else None
        family = await session.get(Family, rule.family_id) if rule else None
    if not user or not rule or not category or rule.family_id != user.family_id:
        await callback.answer("Правило недоступно", show_alert=True); return
    editable = can_manage_family(user)
    def value(field: str) -> str:
        return link(f"rce:{rule.id}:{field}", "✎") if editable else ""
    html = (
        f"<h3>Регулярная операция №{rule.id}</h3><table><tr><th>Поле</th><th>Значение</th><th></th></tr>"
        f"<tr><td>Сумма</td><td>{esc(money(rule.amount, family.currency if family else 'ILS'))}</td><td>{value('amount')}</td></tr>"
        f"<tr><td>Тип</td><td>{'Доход' if rule.type == 'income' else 'Расход'}</td><td>{value('type')}</td></tr>"
        f"<tr><td>Категория</td><td>{esc(category.name)}</td><td>{value('category')}</td></tr>"
        f"<tr><td>Период</td><td>{'Неделя' if rule.cadence == 'weekly' else 'Месяц'}</td><td>{value('cadence')}</td></tr>"
        f"<tr><td>Следующее</td><td>{esc(rule.next_run_at.strftime('%d.%m.%Y %H:%M UTC'))}</td><td>{value('next_run')}</td></tr>"
        f"<tr><td>Комментарий</td><td>{esc(rule.comment or '—')}</td><td>{value('comment')}</td></tr></table>"
    )
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html); await callback.answer()


@router.callback_query(F.data.startswith("rce:"))
async def recurring_edit_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"amount", "type", "category", "cadence", "next_run", "comment", "delete", "confirm_delete"}:
        await callback.answer("Некорректное действие", show_alert=True); return
    rule_id, action = int(parts[1]), parts[2]
    async with SessionLocal() as session, session.begin():
        user = await session.get(User, callback.from_user.id)
        rule = await session.get(RecurringTransaction, rule_id)
        if not user or not rule or rule.family_id != user.family_id or not can_manage_family(user):
            await callback.answer("Недостаточно прав", show_alert=True); return
        if action == "delete":
            html = f"<p>Удалить правило? {link(f'rce:{rule.id}:confirm_delete', 'Да')} · {link(f'rcopen:{rule.id}', 'Нет')}</p>"
        elif action == "confirm_delete":
            await session.delete(rule); html = "<p>Правило удалено.</p>"
        elif action == "type":
            html = f"<p>Тип: {link(f'rcet:{rule.id}:income', 'Доход')} · {link(f'rcet:{rule.id}:expense', 'Расход')}</p>"
        elif action == "category":
            categories = await list_categories(session, rule.family_id, rule.type)
            html = "<p>Категория: " + " · ".join(link(f"rcec:{rule.id}:{item.id}", item.name) for item in categories) + "</p>"
        elif action == "cadence":
            html = f"<p>Период: {link(f'rcep:{rule.id}:weekly', 'Неделя')} · {link(f'rcep:{rule.id}:monthly', 'Месяц')}</p>"
        else:
            await state.update_data(recurring_rule_id=rule.id, recurring_edit_field=action)
            await state.set_state(RecurringEditFlow.waiting_value)
            prompt = {"amount": "Сумма", "next_run": "Дата ДД.ММ.ГГГГ", "comment": "Комментарий"}[action]
            await rich.send(bot, callback.message.chat.id, f"<p>{esc(prompt)}:</p>"); await callback.answer(); return
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html); await callback.answer()


@router.callback_query(F.data.startswith(("rcet:", "rcec:", "rcep:")))
async def recurring_edit_choice(callback: CallbackQuery, bot: Bot) -> None:
    prefix, rule_text, value = callback.data.split(":")
    if not rule_text.isdigit():
        await callback.answer("Некорректное действие", show_alert=True); return
    async with SessionLocal() as session, session.begin():
        user, rule = await session.get(User, callback.from_user.id), await session.get(RecurringTransaction, int(rule_text))
        if not user or not rule or rule.family_id != user.family_id or not can_manage_family(user):
            await callback.answer("Недостаточно прав", show_alert=True); return
        if prefix == "rcet" and value in {"income", "expense"}:
            rule.type = value
            categories = await list_categories(session, rule.family_id, value)
            if not categories: await callback.answer("Нет категорий этого типа", show_alert=True); return
            rule.category_id = categories[0].id
        elif prefix == "rcec" and value.isdigit():
            category = await session.get(Category, int(value))
            if not category or category.family_id != rule.family_id or category.type != rule.type: await callback.answer("Категория недоступна", show_alert=True); return
            rule.category_id = category.id
        elif prefix == "rcep" and value in {"weekly", "monthly"}:
            rule.cadence = value
        else:
            await callback.answer("Некорректное действие", show_alert=True); return
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, f"<p>Сохранено. {link(f'rcopen:{rule_text}', '← К правилу')}</p>"); await callback.answer()


@router.message(RecurringEditFlow.waiting_value)
async def recurring_edit_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data(); rule_id, field = data.get("recurring_rule_id"), data.get("recurring_edit_field")
    if not rule_id or field not in {"amount", "next_run", "comment"}:
        await state.clear(); return
    try:
        async with SessionLocal() as session, session.begin():
            user, rule = await session.get(User, message.from_user.id), await session.get(RecurringTransaction, int(rule_id))
            if not user or not rule or rule.family_id != user.family_id or not can_manage_family(user): raise ValueError("Правило недоступно.")
            if field == "amount":
                value = Decimal((message.text or "").replace(",", "."))
                if value <= 0: raise ValueError("Введите положительную сумму.")
                rule.amount = value
            elif field == "next_run":
                rule.next_run_at = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").replace(tzinfo=timezone.utc)
            else:
                rule.comment = (message.text or "").strip() or None
    except (ValueError, InvalidOperation):
        await rich.send(bot, message.chat.id, "<p>Проверьте значение и повторите.</p>"); return
    await state.clear(); await rich.send(bot, message.chat.id, f"<p>Сохранено. {link(f'rcopen:{rule_id}', '← К правилу')}</p>")


@router.callback_query(F.data.startswith("adv:"))
async def advanced_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    mode = callback.data.split(":", 1)[1]
    if mode == "recurring":
        user = await ensure_callback_user(callback)
        if not can_manage_family(user):
            await callback.answer("У вас роль «Просмотр». Изменять бюджет нельзя.", show_alert=True)
            return
        await state.clear()
        await rich.send(
            bot,
            callback.message.chat.id,
            f"<h3>Новая регулярная операция</h3><p>Шаг 1 из 6. Выберите тип:</p><p>{link('rec:type:expense', 'Расход')} · {link('rec:type:income', 'Доход')} · {link('rec:cancel', 'Отмена')}</p>",
        )
        await callback.answer()
        return
    prompts = {
        "account": "Введите <code>Название; Валюта</code>, например <code>Наличные; RUB</code>.",
        "transfer": "Введите <code>Счёт-откуда; Счёт-куда; Сумма; комментарий</code>.",
        "split": "Введите позиции через <code>;</code>: <code>Категория=сумма; Категория=сумма</code>.",
    }
    if mode not in prompts:
        await callback.answer("Некорректное действие", show_alert=True); return
    await advanced_start(callback, state, bot, mode, prompts[mode])


async def render_recurring_summary(bot: Bot, chat_id: int, data: dict) -> None:
    async with SessionLocal() as session:
        category = await session.get(Category, data.get("recurring_category_id"))
        family = await session.get(Family, data.get("recurring_family_id"))
    if not category or not family:
        await rich.send(bot, chat_id, "<p>Выбранная категория больше недоступна. Начните создание правила заново.</p>")
        return
    cadence = "Каждую неделю" if data["recurring_cadence"] == "weekly" else "Каждый месяц"
    direction = "Доход" if data["recurring_type"] == "income" else "Расход"
    first_run = "Сейчас" if data.get("recurring_run_now") else datetime.fromisoformat(data["recurring_first_run"]).strftime("%d.%m.%Y 09:00")
    await rich.send(
        bot,
        chat_id,
        "<h3>Проверьте правило</h3><table><tr><th>Поле</th><th>Значение</th></tr>"
        f"<tr><td>Тип</td><td>{esc(direction)}</td></tr>"
        f"<tr><td>Сумма</td><td>{esc(money(data['recurring_amount'], family.currency))}</td></tr>"
        f"<tr><td>Категория</td><td>{esc(category.name)}</td></tr>"
        f"<tr><td>Период</td><td>{esc(cadence)}</td></tr>"
        f"<tr><td>Первое выполнение</td><td>{esc(first_run)}</td></tr>"
        f"<tr><td>Комментарий</td><td>{esc(data.get('recurring_comment') or '—')}</td></tr></table>"
        f"<p>{link('rec:save', '✓ Создать правило')} · {link('rec:cancel', 'Отмена')}</p>",
    )


@router.callback_query(F.data.startswith("rec:"))
async def recurring_wizard(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = callback.data.split(":")
    user = await ensure_callback_user(callback)
    if not can_manage_family(user):
        await callback.answer("У вас роль «Просмотр». Изменять бюджет нельзя.", show_alert=True)
        return
    if parts == ["rec", "cancel"]:
        await state.clear()
        await rich.send(bot, callback.message.chat.id, "<p>Создание регулярной операции отменено.</p>")
        await callback.answer()
        return
    data = await state.get_data()
    if len(parts) == 3 and parts[1] == "type" and parts[2] in {"income", "expense"}:
        await state.update_data(recurring_type=parts[2], recurring_family_id=user.family_id)
        await state.set_state(RecurringFlow.waiting_amount)
        await rich.send(bot, callback.message.chat.id, f"<h3>Новая регулярная операция</h3><p>Шаг 2 из 6. Введите сумму {('дохода' if parts[2] == 'income' else 'расхода')}, например <code>1 500,50</code>.</p><p>{link('rec:cancel', 'Отмена')}</p>")
    elif len(parts) == 3 and parts[1] == "category" and parts[2].isdigit():
        async with SessionLocal() as session:
            category = await session.get(Category, int(parts[2]))
        if not category or category.family_id != user.family_id or category.type != data.get("recurring_type") or not category.is_active:
            await callback.answer("Категория недоступна. Начните заново.", show_alert=True)
            return
        await state.update_data(recurring_category_id=category.id)
        await rich.send(bot, callback.message.chat.id, f"<p>Период: {link('rec:cadence:weekly', 'Неделя')} · {link('rec:cadence:monthly', 'Месяц')} · {link('rec:cancel', 'Отмена')}</p>")
    elif len(parts) == 3 and parts[1] == "cadence" and parts[2] in {"weekly", "monthly"}:
        await state.update_data(recurring_cadence=parts[2])
        await state.set_state(RecurringFlow.waiting_first_run)
        await rich.send(bot, callback.message.chat.id, f"<p>Первое выполнение: {link('rec:run:now', 'Сейчас')} или введите дату <code>ДД.ММ.ГГГГ</code>.</p>")
    elif parts == ["rec", "run", "now"]:
        await state.update_data(recurring_first_run=datetime.now(timezone.utc).isoformat(), recurring_run_now=True)
        await rich.send(bot, callback.message.chat.id, f"<h3>Новая регулярная операция</h3><p>Шаг 6 из 6. Добавьте комментарий или пропустите.</p><p>{link('rec:comment:skip', 'Пропустить')} · {link('rec:cancel', 'Отмена')}</p>")
        await state.set_state(RecurringFlow.waiting_comment)
    elif parts == ["rec", "comment", "skip"]:
        await state.update_data(recurring_comment=None)
        await state.set_state(None)
        await render_recurring_summary(bot, callback.message.chat.id, await state.get_data())
    elif parts == ["rec", "save"]:
        required = {"recurring_type", "recurring_amount", "recurring_category_id", "recurring_cadence", "recurring_first_run"}
        if not required.issubset(data):
            await callback.answer("Экран устарел. Начните создание правила заново.", show_alert=True)
            return
        async with SessionLocal() as session, session.begin():
            category = await session.get(Category, data["recurring_category_id"])
            if not category or category.family_id != user.family_id or category.type != data["recurring_type"]:
                await callback.answer("Категория недоступна.", show_alert=True)
                return
            first_run = datetime.fromisoformat(data["recurring_first_run"])
            recurring = await create_recurring_transaction(session, user.family_id, user.id, category, Decimal(data["recurring_amount"]), data["recurring_type"], data["recurring_cadence"], first_run, comment=data.get("recurring_comment"))
            materialized = bool(data.get("recurring_run_now"))
            if materialized:
                await materialize_recurring_transaction(session, recurring)
            next_run = recurring.next_run_at
            category_name, amount, tx_type, comment = category.name, recurring.amount, recurring.type, recurring.comment
        await state.clear()
        if materialized:
            await rich.send(bot, callback.message.chat.id, f"<p>Правило создано; первая операция записана. Следующее: <code>{esc(next_run.strftime('%d.%m.%Y %H:%M UTC'))}</code>.</p>")
            await notify_other_family_members(bot, user.family_id, user.id, [(amount, tx_type, category_name, comment)])
            await send_current_month_report(bot, callback.message.chat.id, user.id, user.family_id)
        else:
            await rich.send(bot, callback.message.chat.id, f"<p>Правило создано. Первое выполнение: <code>{esc(first_run.strftime('%d.%m.%Y %H:%M UTC'))}</code>.</p>")
    else:
        await callback.answer("Этот шаг устарел. Начните создание правила заново.", show_alert=True)
        return
    await callback.answer()


@router.message(RecurringFlow.waiting_amount)
async def recurring_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    user = await ensure_user(message)
    data = await state.get_data()
    raw_amount = re.sub(r"[^0-9,.]", "", message.text or "").replace(",", ".")
    try:
        amount = Decimal(raw_amount)
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await rich.send(bot, message.chat.id, "<p>Введите положительную сумму, например <code>1 500,50</code>.</p>")
        return
    if not can_manage_family(user) or data.get("recurring_type") not in {"income", "expense"}:
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Сессия создания устарела. Откройте /recurring заново.</p>")
        return
    async with SessionLocal() as session:
        categories = await list_categories(session, user.family_id, data["recurring_type"])
    if not categories:
        await rich.send(bot, message.chat.id, "<p>Нет активных категорий нужного типа. Создайте её в /categories и начните заново.</p>")
        return
    await state.update_data(recurring_amount=str(amount))
    await state.set_state(None)
    choices = " · ".join(link(f"rec:category:{category.id}", category.name) for category in categories)
    await rich.send(bot, message.chat.id, f"<h3>Новая регулярная операция</h3><p>Шаг 3 из 6. Выберите категорию:</p><p>{choices}</p><p>{link('rec:cancel', 'Отмена')}</p>")


@router.message(RecurringFlow.waiting_first_run)
async def recurring_first_run(message: Message, state: FSMContext, bot: Bot) -> None:
    user = await ensure_user(message)
    try:
        chosen_date = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
        async with SessionLocal() as session:
            family = await session.get(Family, user.family_id)
        first_run = datetime.combine(chosen_date, time(9, 0), tzinfo=ZoneInfo(family.timezone if family else "Asia/Jerusalem")).astimezone(timezone.utc)
    except ValueError:
        await rich.send(bot, message.chat.id, "<p>Введите дату как <code>10.10.2026</code> или выберите «Сейчас».</p>")
        return
    if first_run <= datetime.now(timezone.utc):
        await rich.send(bot, message.chat.id, "<p>Укажите будущую дату или выберите «Сейчас».</p>")
        return
    await state.update_data(recurring_first_run=first_run.isoformat(), recurring_run_now=False)
    await state.set_state(RecurringFlow.waiting_comment)
    await rich.send(bot, message.chat.id, f"<p>Комментарий: {link('rec:comment:skip', 'Пропустить')} или введите текст.</p>")


@router.message(RecurringFlow.waiting_comment)
async def recurring_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    comment = (message.text or "").strip()
    if len(comment) > 2000:
        await rich.send(bot, message.chat.id, "<p>Комментарий не должен быть длиннее 2000 символов.</p>")
        return
    await state.update_data(recurring_comment=comment or None)
    await state.set_state(None)
    await render_recurring_summary(bot, message.chat.id, await state.get_data())


@router.message(Command("transactions"))
async def transactions(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>")
        return
    screen = await create_screen(user, message.chat.id, "transactions", {"page": "0", "sort": "date", "descending": "1"})
    await render_transactions(bot, screen)


async def render_transactions(bot: Bot, screen: InteractiveScreen, edit: bool = False) -> None:
    state = screen.state
    page = max(0, int(state.get("page", "0")))
    sort = state.get("sort", "date")
    descending = state.get("descending", "1") == "1"
    async with SessionLocal() as session:
        conditions = [Transaction.family_id == screen.family_id, Transaction.deleted_at.is_(None)]
        if state.get("type") in {"income", "expense"}:
            conditions.append(Transaction.type == state["type"])
        if state.get("category", "").isdigit():
            conditions.append(Transaction.category_id == int(state["category"]))
        ordering = {
            "amount": Transaction.amount,
            "category": Category.name,
            "date": Transaction.date,
        }.get(sort, Transaction.date)
        rows = await session.execute(
            select(Transaction, Category.name, User.username)
            .join(Category, Category.id == Transaction.category_id)
            .outerjoin(User, User.id == Transaction.user_id)
            .where(*conditions)
            .order_by(ordering.desc() if descending else ordering.asc(), Transaction.id.desc())
            .offset(page * 20)
            .limit(21)
        )
        items = list(rows)
        categories = await list_categories(session, screen.family_id)
        family = await session.get(Family, screen.family_id)
        viewer = await session.get(User, screen.user_id)
    local_zone = ZoneInfo(family.timezone if family else "UTC")
    currency = family.currency if family else "ILS"
    more = len(items) > 20
    items = items[:20]
    body = "".join(
        "<tr>"
        f"<td>{link(f'txopen:{tx.id}', tx.date.astimezone(local_zone).strftime('%d.%m %H:%M'))}</td>"
        f"<td>{esc(category)}<br/>{esc(tx.comment or '—')}</td>"
        f"<td>{link(f'txopen:{tx.id}', ('+' if tx.type == 'income' else '−') + money(tx.amount, currency))}</td>"
        f"<td>{link(f'txe:{tx.id}:delete', '🗑') if can_manage_family(viewer) else ''}</td>"
        "</tr>"
        for tx, category, _username in items
    ) or '<tr><td colspan="4">Операций пока нет.</td></tr>'
    def sort_link(field: str, title: str) -> str:
        marker = " ↓" if sort == field and descending else " ↑" if sort == field else " ↕"
        return link(f"t:{screen.token}:s:{field}", title + marker)
    filter_name = "Все" if not state.get("type") else ("Расходы" if state["type"] == "expense" else "Доходы")
    cat_name = next((item.name for item in categories if str(item.id) == state.get("category")), "Все категории")
    nav = [link(f"t:{screen.token}:p:prev", "←")]
    if more: nav.append(link(f"t:{screen.token}:p:next", "→"))
    html = (
        f"<h3>Операции · страница {page + 1}</h3>"
        f"<p>{link(f't:{screen.token}:f:type', filter_name)} · {link(f't:{screen.token}:f:category', cat_name)} · {' · '.join(nav)}</p>"
        f"<table><tr><th>{sort_link('date', 'Дата')}</th><th>{sort_link('category', 'Категория')}</th><th>{sort_link('amount', 'Сумма')}</th><th></th></tr>{body}</table>"
    )
    if edit and screen.message_id:
        await rich.edit(bot, screen.chat_id, screen.message_id, html)
    else:
        result = await rich.send(bot, screen.chat_id, html)
        async with SessionLocal() as session, session.begin():
            stored = await session.get(InteractiveScreen, screen.id)
            stored.message_id = result.get("message_id")


@router.callback_query(F.data.startswith("txopen:"))
async def transaction_detail(callback: CallbackQuery, bot: Bot) -> None:
    raw_id = callback.data.split(":", 1)[1]
    if not raw_id.isdigit():
        await callback.answer("Некорректная операция", show_alert=True)
        return
    async with SessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        tx = await session.get(Transaction, int(raw_id))
        category = await session.get(Category, tx.category_id) if tx else None
        family = await session.get(Family, tx.family_id) if tx else None
        parent = await session.scalar(
            select(InteractiveScreen).where(
                InteractiveScreen.chat_id == callback.message.chat.id,
                InteractiveScreen.message_id == callback.message.message_id,
                InteractiveScreen.user_id == callback.from_user.id,
                InteractiveScreen.kind.in_(("transactions", "report")),
            )
        )
    if not user or not user.family_id or not tx or tx.deleted_at is not None or tx.family_id != user.family_id or not category:
        await callback.answer("Операция недоступна", show_alert=True)
        return
    receipt = (
        link(f"receipt:{tx.id}:retry", "Повторно распознать чек")
        if tx.receipt_file_id
        else "Чек не прикреплён"
    )
    html = (
        f"<h3>Операция №{tx.id}</h3><table><tr><th>Поле</th><th>Значение</th><th></th></tr>"
        f"<tr><td>Сумма</td><td>{esc(money(tx.amount, family.currency if family else 'ILS'))}</td><td>{link(f'txe:{tx.id}:amount', '✎') if can_manage_family(user) else ''}</td></tr>"
        f"<tr><td>Тип</td><td>{'Доход' if tx.type == 'income' else 'Расход'}</td><td>{link(f'txe:{tx.id}:type', '✎') if can_manage_family(user) else ''}</td></tr>"
        f"<tr><td>Категория</td><td>{esc(category.name)}</td><td>{link(f'txe:{tx.id}:category', '✎') if can_manage_family(user) else ''}</td></tr>"
        f"<tr><td>Комментарий</td><td>{esc(tx.comment or '—')}</td><td>{link(f'txe:{tx.id}:comment', '✎') if can_manage_family(user) else ''}</td></tr>"
        f"<tr><td>Чек</td><td>{receipt}</td><td></td></tr></table>"
    )
    if can_manage_family(user):
        html += f"<p>Дата: {esc(tx.date.strftime('%d.%m.%Y %H:%M'))} {link(f'txe:{tx.id}:date', '✎')}</p>"
    if parent:
        action = "r" if parent.kind == "report" else "t"
        html += f"<p>{link(f'{action}:{parent.token}:b', '← Назад')}</p>"
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
    await callback.answer()


@router.callback_query(F.data.startswith("t:"))
async def transactions_action(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    _, token, action, *args = parts
    async with SessionLocal() as session, session.begin():
        screen = await session.scalar(select(InteractiveScreen).where(InteractiveScreen.token == token))
        user = await session.get(User, callback.from_user.id)
        if not screen or screen.kind != "transactions" or not user or screen.user_id != user.id or screen.family_id != user.family_id or screen.chat_id != callback.message.chat.id or screen.message_id != callback.message.message_id or screen.expires_at <= datetime.now(timezone.utc):
            await callback.answer("Экран устарел. Откройте /transactions заново.", show_alert=True)
            return
        state = dict(screen.state)
        if action == "s" and args and args[0] in {"date", "amount", "category"}:
            state["descending"] = "0" if state.get("sort") == args[0] and state.get("descending") == "1" else "1"
            state["sort"] = args[0]
        elif action == "p" and args and args[0] in {"prev", "next"}:
            state["page"] = str(max(0, int(state.get("page", "0")) + (-1 if args[0] == "prev" else 1)))
        elif action == "f" and args and args[0] in {"type", "category"}:
            state["filter_picker"] = args[0]
        elif action == "ft" and args and args[0] in {"all", "income", "expense"}:
            if args[0] == "all": state.pop("type", None)
            else: state["type"] = args[0]
            state.pop("filter_picker", None); state["page"] = "0"
        elif action == "fc" and args and args[0] == "all":
            state.pop("category", None); state.pop("filter_picker", None); state["page"] = "0"
        elif action == "fc" and args and args[0].isdigit():
            state["category"] = args[0]; state.pop("filter_picker", None); state["page"] = "0"
        elif action == "b": state.pop("filter_picker", None)
        else:
            await callback.answer("Некорректное действие", show_alert=True)
            return
        screen.state, screen.token, screen.revision, screen.expires_at = state, secrets.token_urlsafe(12), screen.revision + 1, expiry()
        await session.flush()
        shadow = InteractiveScreen(id=screen.id, token=screen.token, family_id=screen.family_id, chat_id=screen.chat_id, message_id=screen.message_id, state=state)
    if state.get("filter_picker"):
        async with SessionLocal() as session:
            cats = await list_categories(session, shadow.family_id)
        if state["filter_picker"] == "type":
            html = "<h3>Тип операций</h3><p>" + " · ".join(link(f"t:{shadow.token}:ft:{key}", name) for key, name in (("all", "Все"), ("expense", "Расходы"), ("income", "Доходы"))) + f"</p><p>{link(f't:{shadow.token}:b', '← К операциям')}</p>"
        else:
            html = "<h3>Категория</h3><p>" + " · ".join([link(f"t:{shadow.token}:fc:all", "Все")] + [link(f"t:{shadow.token}:fc:{cat.id}", cat.name) for cat in cats]) + f"</p><p>{link(f't:{shadow.token}:b', '← К операциям')}</p>"
        await rich.edit(bot, shadow.chat_id, shadow.message_id, html)
    else:
        await render_transactions(bot, shadow, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("receipt:"))
async def receipt_retry(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] != "retry" or not parts[1].isdigit():
        await callback.answer("Некорректное действие", show_alert=True)
        return
    async with SessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        tx = await session.get(Transaction, int(parts[1]))
        if not user or not user.family_id or not tx or tx.family_id != user.family_id or not tx.receipt_file_id:
            await callback.answer("Чек недоступен", show_alert=True)
            return
        names = [item.name for item in await list_categories(session, user.family_id)]
        runtime = await get_family_ai_settings(session, user.family_id)
    await callback.answer("Повторно распознаю чек…")
    file = await bot.get_file(tx.receipt_file_id)
    content = await bot.download_file(file.file_path)
    try:
        parsed = await parse_receipt_file(content.read(), tx.receipt_mime_type or "image/jpeg", names, runtime)
    except (ReceiptError, AIUnavailableError) as error:
        await rich.send(bot, callback.message.chat.id, f"<p>{esc(error)}</p>")
        return
    if not parsed:
        await rich.send(bot, callback.message.chat.id, "<p>Не удалось повторно распознать чек. Исходная операция не изменена.</p>")
        return
    await ask_confirmation(
        callback.message,
        bot,
        parsed,
        receipt_file_id=tx.receipt_file_id,
        receipt_mime_type=tx.receipt_mime_type,
        user=user,
    )


@router.callback_query(F.data.startswith("txet:"))
async def saved_transaction_type(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"income", "expense"}:
        await callback.answer("Некорректный тип", show_alert=True); return
    async with SessionLocal() as session, session.begin():
        user = await session.get(User, callback.from_user.id)
        tx = await session.get(Transaction, int(parts[1]))
        if not user or not tx or tx.family_id != user.family_id or not can_manage_family(user):
            await callback.answer("Недостаточно прав", show_alert=True); return
        categories = await list_categories(session, tx.family_id, parts[2])
        if not categories:
            await callback.answer("Нет активных категорий этого типа", show_alert=True); return
        await edit_transaction(session, tx, user.id, tx_type=parts[2], category_id=categories[0].id)
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, "<p>Тип и совместимая категория обновлены.</p>")
    await callback.answer()


@router.callback_query(F.data.startswith("txec:"))
async def saved_transaction_category(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await callback.answer("Некорректная категория", show_alert=True); return
    async with SessionLocal() as session, session.begin():
        user = await session.get(User, callback.from_user.id)
        tx, category = await session.get(Transaction, int(parts[1])), await session.get(Category, int(parts[2]))
        if not user or not tx or not category or tx.family_id != user.family_id or category.family_id != tx.family_id or category.type != tx.type or not can_manage_family(user):
            await callback.answer("Категория недоступна", show_alert=True); return
        await edit_transaction(session, tx, user.id, category_id=category.id)
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, "<p>Категория обновлена.</p>")
    await callback.answer()


@router.callback_query(F.data.startswith("txe:"))
async def saved_transaction_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"amount", "type", "category", "date", "comment", "delete", "confirm_delete"}:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    tx_id, action = int(parts[1]), parts[2]
    async with SessionLocal() as session, session.begin():
        user = await session.get(User, callback.from_user.id)
        tx = await session.get(Transaction, tx_id)
        if not user or not tx or tx.family_id != user.family_id or not can_manage_family(user):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        if action == "delete":
            await rich.edit(bot, callback.message.chat.id, callback.message.message_id, f"<p>Удалить операцию №{tx.id}? {link(f'txe:{tx.id}:confirm_delete', 'Да, удалить')} · {link(f'txopen:{tx.id}', 'Отмена')}</p>")
            await callback.answer()
            return
        if action == "confirm_delete":
            await delete_transaction(session, tx, user.id)
            html = "<p>Операция удалена.</p>"
        elif action == "type":
            html = f"<h3>Выберите тип</h3><p>{link(f'txet:{tx.id}:income', 'Доход')} · {link(f'txet:{tx.id}:expense', 'Расход')}</p>"
        elif action == "category":
            categories = await list_categories(session, tx.family_id, tx.type)
            html = "<h3>Выберите категорию</h3><p>" + " · ".join(link(f"txec:{tx.id}:{item.id}", item.name) for item in categories) + "</p>"
        else:
            await state.update_data(saved_transaction_id=tx.id, saved_transaction_field=action)
            await state.set_state(TransactionEditFlow.waiting_value)
            prompt = {"amount": "Введите новую сумму положительным числом.", "date": "Введите дату и время в формате <code>2026-09-17 14:30</code> (UTC).", "comment": "Введите новый комментарий."}[action]
            await rich.send(bot, callback.message.chat.id, f"<p>{prompt}</p>")
            await callback.answer()
            return
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
    await callback.answer()


@router.message(Command("budget"))
async def budget(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>")
        return
    async with SessionLocal() as session:
        html = await budget_html(session, user)
    await rich.send(bot, message.chat.id, html)


async def budget_html(session, user: User) -> str:
    """Render the same budget table for initial display and inline refresh."""
    items = await list_categories(session, user.family_id, "expense")
    family = await session.get(Family, user.family_id)
    _, report, _, _, _ = await report_rows(
        session, user.family_id, "month", datetime.now(timezone.utc).strftime("%Y-%m")
    )
    values = {item["id"]: item for item in report if item["type"] == "expense"}
    rows = "".join(
        "<tr>"
        f"<td>{link(f'budget:{item.id}', item.name) if can_manage_family(user) else esc(item.name)}</td>"
        f"<td>{esc(money(values.get(item.id, {}).get('plan', 0), family.currency if family else 'ILS'))}</td>"
        f"<td>{esc(money(values.get(item.id, {}).get('fact', 0), family.currency if family else 'ILS'))}</td>"
        f"<td>{esc(money(values.get(item.id, {}).get('delta', 0), family.currency if family else 'ILS'))}</td></tr>"
        for item in items
    ) or '<tr><td colspan="4">Нет активных расходных категорий.</td></tr>'
    hint = "Нажмите категорию, чтобы изменить лимит." if can_manage_family(user) else "Лимиты изменяет owner или editor."
    return f"<h3>Лимиты · текущий месяц</h3><table><tr><th>Категория</th><th>План</th><th>Факт</th><th>Остаток</th></tr>{rows}</table><p>{hint}</p>"


@router.callback_query(F.data.startswith("r:"))
async def report_action(callback: CallbackQuery, bot: Bot) -> None:
    _, token, action, *args = callback.data.split(":")
    async with SessionLocal() as session, session.begin():
        screen = await session.scalar(select(InteractiveScreen).where(InteractiveScreen.token == token))
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
        if (
            not screen
            or screen.kind != "report"
            or screen.user_id != user.id
            or screen.family_id != user.family_id
            or screen.chat_id != callback.message.chat.id
            or screen.expires_at <= datetime.now(timezone.utc)
            or screen.message_id != callback.message.message_id
        ):
            await callback.answer("Экран устарел. Откройте /report заново.", show_alert=True)
            return
        state = dict(screen.state)
        if action == "p" and args and args[0] in {"prev", "next"}:
            candidate = shift_anchor(state["anchor"], state["kind"], -1 if args[0] == "prev" else 1)
            family = await session.get(Family, screen.family_id)
            current_anchor = datetime.now(ZoneInfo(family.timezone if family else "UTC")).strftime("%Y-%m")
            if args[0] == "next" and candidate > current_anchor:
                await callback.answer("Будущие периоды недоступны", show_alert=True)
                return
            state["anchor"] = candidate
        elif action == "k" and args and args[0] in {"month", "quarter", "year"}:
            state["kind"] = args[0]
        elif action == "v":
            state["period_picker"] = "1"
            state.setdefault("picker_year", state["anchor"][:4])
        elif action == "pk" and args and args[0] in {"month", "quarter", "year"}:
            state["kind"] = args[0]
        elif action == "py" and args and args[0] in {"prev", "next"}:
            state["picker_year"] = str(int(state.get("picker_year", state["anchor"][:4])) + (-1 if args[0] == "prev" else 1))
        elif action == "ps" and args and re.fullmatch(r"\d{4}-\d{2}", args[0]):
            state["anchor"] = args[0]
            state.pop("period_picker", None); state.pop("picker_year", None)
        elif action == "pt":
            family = await session.get(Family, screen.family_id)
            state["anchor"] = datetime.now(ZoneInfo(family.timezone if family else "UTC")).strftime("%Y-%m")
            state.pop("period_picker", None); state.pop("picker_year", None)
        elif action == "s":
            if not args or args[0] not in {"name", "plan", "fact", "delta"}:
                await callback.answer("Некорректное действие", show_alert=True)
                return
            field = args[0]; state["descending"] = "0" if state.get("sort") == field and state.get("descending") == "1" else "1"; state["sort"] = field
        elif action == "e" and args and args[0] in {"income", "expense"}:
            if state.get("open_type") == args[0]:
                state.pop("open_type", None); state.pop("open_category", None)
            else:
                state["open_type"] = args[0]; state.pop("open_category", None)
        elif action == "c" and args and args[0].isdigit():
            category_id = int(args[0])
            valid_category = await session.scalar(select(Category.id).where(Category.id == category_id, Category.family_id == screen.family_id))
            if not valid_category:
                await callback.answer("Категория недоступна", show_alert=True)
                return
            if state.get("open_category") == args[0]: state.pop("open_category", None)
            else: state["open_category"] = args[0]
        elif action == "b":
            # The operation card replaces the report message.  Returning does
            # not change report state; it simply renders the same inline tree.
            state.pop("period_picker", None); state.pop("picker_year", None)
        elif action != "x":
            await callback.answer("Некорректное действие", show_alert=True)
            return
        screen.state, screen.revision, screen.expires_at = state, screen.revision + 1, expiry()
        # Rotate the opaque capability after every edit: old links cannot replay
        # a stale snapshot/revision even if their message remains cached.
        screen.token = secrets.token_urlsafe(12)
        await session.flush()
        screen_id, message_id, chat_id, family_id, current, next_token = screen.id, screen.message_id, screen.chat_id, screen.family_id, dict(screen.state), screen.token
    shadow = InteractiveScreen(
        id=screen_id, token=next_token, user_id=user.id, family_id=family_id,
        chat_id=chat_id, message_id=message_id, state=current,
    )
    if current.get("period_picker"):
        async with SessionLocal() as session:
            body = await report_period_picker_html(session, shadow)
        await rich.edit(bot, chat_id, message_id, body)
    else:
        await render_report(bot, shadow, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("family:"))
async def family_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if callback.data == "family:join":
        await state.set_state(JoinFamily.waiting_code)
        await rich.send(bot, callback.message.chat.id, "<p>Введите код приглашения.</p>")
    elif callback.data == "family:create":
        async with SessionLocal() as session, session.begin():
            user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
            family = await create_family_for_user(session, user, f"Семья {callback.from_user.first_name or callback.from_user.id}")
        await rich.edit(bot, callback.message.chat.id, callback.message.message_id, f"<h3>Готово</h3><p>Код приглашения: <code>{esc(family.invite_code)}</code></p>")
    elif callback.data == "family:notify":
        async with SessionLocal() as session, session.begin():
            user = await session.get(User, callback.from_user.id)
            family = await session.get(Family, user.family_id) if user and user.family_id else None
            if not user or user.role != "owner" or not family:
                await callback.answer("Только владелец может менять настройки семьи.", show_alert=True)
                return
            family.notify_members_on_transactions = not family.notify_members_on_transactions
            status = "включены" if family.notify_members_on_transactions else "выключены"
        await rich.edit(bot, callback.message.chat.id, callback.message.message_id, f"<p>Уведомления о новых операциях {status}.</p>")
    elif callback.data == "family:openai_model":
        async with SessionLocal() as session:
            user = await session.get(User, callback.from_user.id)
        if not user or user.role != "owner" or not user.family_id:
            await callback.answer("Только владелец может менять настройки семьи.", show_alert=True)
            return
        await render_family_model_choices(bot, callback.message.chat.id, user.family_id)
    elif callback.data.startswith("family:model_page:"):
        raw_page = callback.data.removeprefix("family:model_page:")
        async with SessionLocal() as session:
            user = await session.get(User, callback.from_user.id)
        if not user or user.role != "owner" or not user.family_id or not raw_page.isdigit():
            await callback.answer("Только владелец может менять настройки семьи.", show_alert=True)
            return
        await render_family_model_choices(bot, callback.message.chat.id, user.family_id, int(raw_page))
    elif callback.data.startswith("family:model:"):
        model_id = callback.data.removeprefix("family:model:")
        async with SessionLocal() as session, session.begin():
            user = await session.get(User, callback.from_user.id)
            family = await session.get(Family, user.family_id) if user and user.family_id else None
            if not user or user.role != "owner" or not family:
                await callback.answer("Только владелец может менять настройки семьи.", show_alert=True)
                return
            try:
                await set_family_openai_model(session, family, model_id)
            except ValueError as error:
                await callback.answer(str(error), show_alert=True)
                return
        await rich.send(bot, callback.message.chat.id, f"<p>Модель семьи <code>{esc(model_id)}</code> сохранена.</p>")
    elif callback.data in {"family:timezone", "family:currency", "family:retention", "family:openai_key"}:
        async with SessionLocal() as session:
            user = await session.get(User, callback.from_user.id)
        if not user or user.role != "owner" or not user.family_id:
            await callback.answer("Только владелец может менять настройки семьи.", show_alert=True)
            return
        field = callback.data.split(":", 1)[1]
        await state.update_data(family_setting=field)
        await state.set_state(FamilySettingsFlow.waiting_value)
        prompt = {
            "timezone": "Введите IANA-часовой пояс, например <code>Asia/Jerusalem</code>.",
            "currency": "Введите трёхбуквенный ISO-код валюты, например <code>RUB</code>.",
            "retention": "Введите срок хранения Telegram file_id чека в днях: от <code>0</code> (не хранить) до <code>3650</code>.",
            "openai_key": "Введите OpenAI API key семьи. Он будет зашифрован и никогда не выводится в чат.",
        }[field]
        await rich.send(bot, callback.message.chat.id, f"<p>{prompt}</p>")
    else:
        parts = callback.data.split(":")
        async with SessionLocal() as session, session.begin():
            user = await session.get(User, callback.from_user.id)
            if not user or user.role != "owner" or not user.family_id:
                await callback.answer("Только владелец может управлять приглашениями.", show_alert=True)
                return
            if len(parts) == 3 and parts[1] == "invite" and parts[2] in {"viewer", "editor"}:
                invite = await create_family_invite(session, user.family_id, user.id, role=parts[2])
                url = invitation_url((await bot.get_me()).username or "", invite.code)
                html = f"<h3>Приглашение создано</h3><p>Роль: {esc(parts[2])}<br/>Перешлите эту ссылку: {invitation_anchor(url)}</p>"
            elif callback.data == "family:rotate":
                invite = await rotate_family_invite(session, user.family_id, user.id)
                url = invitation_url((await bot.get_me()).username or "", invite.code)
                html = f"<h3>Ссылка сменена</h3><p>Старые ссылки отозваны.<br/>Новая ссылка: {invitation_anchor(url)}</p>"
            elif len(parts) == 3 and parts[1] == "revoke" and parts[2].isdigit():
                from app.repositories import revoke_family_invite
                if not await revoke_family_invite(session, user.family_id, int(parts[2]), user.id):
                    await callback.answer("Приглашение уже недоступно.", show_alert=True)
                    return
                html = "<p>Приглашение отозвано.</p>"
            else:
                await callback.answer("Некорректное действие", show_alert=True)
                return
        await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
    await callback.answer()


@router.message(FamilySettingsFlow.waiting_value)
async def family_setting_value(message: Message, state: FSMContext, bot: Bot) -> None:
    field = (await state.get_data()).get("family_setting")
    user = await ensure_user(message)
    if field not in {"timezone", "currency", "retention", "openai_key"} or user.role != "owner" or not user.family_id:
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Сессия настройки недоступна.</p>")
        return
    try:
        async with SessionLocal() as session, session.begin():
            family = await session.get(Family, user.family_id)
            if field == "timezone":
                await update_family_preferences(session, family, timezone_name=(message.text or "").strip())
            elif field == "currency":
                await update_family_preferences(session, family, currency=(message.text or "").strip())
            elif field == "openai_key":
                await set_family_openai_api_key(session, family, message.text or "")
            else:
                await set_receipt_retention_policy(session, family, int((message.text or "").strip()))
    except (ValueError, KeyError) as error:
        await rich.send(bot, message.chat.id, f"<p>{esc(error)}</p>")
        return
    await state.clear()
    await rich.send(bot, message.chat.id, "<p>Настройка семьи сохранена.</p>")


@router.message(Command("family"))
async def family_screen(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала создайте семью через /start.</p>")
        return
    async with SessionLocal() as session:
        family = await session.get(Family, user.family_id)
        members = list(await session.scalars(select(User).where(User.family_id == user.family_id).order_by(User.role.desc(), User.username, User.id)))
        invites = []
        if user.role == "owner":
            invites = list(await session.scalars(select(FamilyInvite).where(FamilyInvite.family_id == user.family_id, FamilyInvite.revoked_at.is_(None)).order_by(FamilyInvite.created_at.desc()).limit(5)))
    member_rows = "".join(f"<tr><td>{esc(item.username or item.id)}</td><td>{esc(item.role)}</td></tr>" for item in members)
    invite_section = "<p>Приглашениями управляет владелец.</p>"
    if user.role == "owner":
        bot_username = (await bot.get_me()).username or ""
        invite_rows = "".join(f"<tr><td>{invitation_anchor(invitation_url(bot_username, item.code))}</td><td>{esc(item.role)}</td><td>{item.uses_count}{'/' + str(item.max_uses) if item.max_uses else ''}</td><td>{link(f'family:revoke:{item.id}', 'Отозвать')}</td></tr>" for item in invites) or '<tr><td colspan="4">Нет активных приглашений.</td></tr>'
        notifications = "включены" if family and family.notify_members_on_transactions else "выключены"
        ai_key_status = "настроен" if family and family.openai_api_key else "не задан"
        ai_model = family.openai_model if family else "gpt-5.4"
        invite_section = f"<h3>Приглашения</h3><table><tr><th>Код</th><th>Роль</th><th>Использования</th><th></th></tr>{invite_rows}</table><p>{link('family:invite:viewer', 'Новое приглашение')} · {link('family:invite:editor', 'Пригласить редактора')} · {link('family:rotate', 'Сменить общий код')}</p><h3>Настройки семьи</h3><p>{link('family:timezone', 'Часовой пояс')} · {link('family:currency', 'Валюта')} · {link('family:retention', 'Срок хранения чеков')}</p><p>OpenAI key: <b>{ai_key_status}</b> · {link('family:openai_key', 'Изменить')}</p><p>Модель: <code>{esc(ai_model)}</code> · {link('family:openai_model', 'Выбрать')}</p><p>Уведомления о новых операциях: <b>{notifications}</b> · {link('family:notify', 'Изменить')}</p>"
    await rich.send(bot, message.chat.id, f"<h3>{esc(family.name if family else 'Семья')}</h3><p>Часовой пояс: <code>{esc(family.timezone if family else '')}</code> · Валюта: <code>{esc(family.currency if family else '')}</code> · Чеки: <code>{esc(family.receipt_retention_days if family else 0)} дней</code></p><h3>Участники</h3><table><tr><th>Имя</th><th>Роль</th></tr>{member_rows}</table>{invite_section}")


@router.message(JoinFamily.waiting_code)
async def join_code(message: Message, state: FSMContext, bot: Bot) -> None:
    async with SessionLocal() as session, session.begin():
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        family = await join_family(session, user, message.text or "")
    await state.clear()
    await rich.send(bot, message.chat.id, f"<p>{'Вы подключены к бюджету: ' + esc(family.name) if family else 'Код не найден. Попробуйте еще раз.'}</p>")


@router.callback_query(F.data.startswith("budget:"))
async def budget_pick(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    raw_action = callback.data.split(":")[1]
    if raw_action == "copy":
        async with SessionLocal() as session, session.begin():
            user = await session.get(User, callback.from_user.id)
            if not can_manage_family(user):
                await callback.answer("Недостаточно прав.", show_alert=True); return
            now = datetime.now(timezone.utc)
            target = now.strftime("%Y-%m")
            previous = shift_anchor(target, "month", -1)
            copied = await copy_budgets_from_period(session, user.family_id, previous, target)
        await rich.edit(bot, callback.message.chat.id, callback.message.message_id, f"<p>Скопировано лимитов: {copied}. Существующие лимиты текущего месяца не изменены.</p>")
        await callback.answer()
        return
    category_id = raw_action
    if not category_id.isdigit():
        await callback.answer("Некорректная категория", show_alert=True)
        return
    async with SessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        category = await session.get(Category, int(category_id))
    if not can_manage_family(user) or not category or category.family_id != user.family_id or category.type != "expense" or not category.is_active:
        await callback.answer("Категория недоступна", show_alert=True)
        return
    await state.update_data(category_id=category.id, budget_chat_id=callback.message.chat.id, budget_message_id=callback.message.message_id)
    await state.set_state(BudgetFlow.waiting_amount)
    prompt = await rich.send(bot, callback.message.chat.id, "<p>Введите лимит положительным числом.</p>")
    await state.update_data(budget_prompt_message_id=prompt.get("message_id"))
    await callback.answer()


@router.message(BudgetFlow.waiting_amount)
async def budget_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        amount = Decimal((message.text or "").replace(",", "."))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await rich.send(bot, message.chat.id, "<p>Введите положительную сумму, например <code>30000</code>.</p>")
        return
    user, data = await ensure_user(message), await state.get_data()
    if not can_manage_family(user):
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Недостаточно прав.</p>")
        return
    async with SessionLocal() as session, session.begin():
        await upsert_budget(session, user.family_id, data["category_id"], amount)
        html = await budget_html(session, user)
    for message_id in (message.message_id, data.get("budget_prompt_message_id")):
        if message_id:
            with suppress(Exception):
                await bot.delete_message(message.chat.id, int(message_id))
    await state.clear()
    if data.get("budget_message_id"):
        await rich.edit(bot, message.chat.id, int(data["budget_message_id"]), html)
    else:
        await rich.send(bot, message.chat.id, html)


async def ask_confirmation(
    message: Message,
    bot: Bot,
    parsed: ParsedTransaction,
    receipt_file_id: str | None = None,
    receipt_mime_type: str | None = None,
    choose_category: bool = False,
    user: User | None = None,
) -> None:
    # Callback messages belong to the bot; callers then pass the person who
    # pressed the rich link so no bot-as-viewer record is used.
    user = user or await ensure_user(message)
    if not can_manage_family(user):
        await rich.send(bot, message.chat.id, "<p>Роль viewer может только просматривать данные.</p>")
        return
    token = secrets.token_urlsafe(12)
    async with SessionLocal() as session, session.begin():
        values = await get_values(session)
        family = await session.get(Family, user.family_id)
        if values.get("receipt_storage") == "do_not_retain" or not family or family.receipt_retention_days == 0:
            receipt_file_id, receipt_mime_type = None, None
        session.add(TransactionDraft(token=token, user_id=user.id, family_id=user.family_id, chat_id=message.chat.id, message_id=None, amount=parsed.amount, source_amount=parsed.amount, source_currency=family.currency if family else "ILS", exchange_rate=Decimal("1"), exchange_rate_date=datetime.now(timezone.utc).date(), exchange_rate_source="family_currency", type=parsed.type, category=parsed.category, comment=parsed.comment, receipt_file_id=receipt_file_id, receipt_mime_type=receipt_mime_type, items=[item.model_dump(mode="json") for item in parsed.items] or None, receipt_discount=parsed.receipt_discount, expires_at=expiry()))
    if choose_category:
        async with SessionLocal() as session:
            categories = await list_categories(session, user.family_id, parsed.type)
        html = "<h3>Выберите категорию</h3><p>" + " · ".join(
            link(f"txc:{token}:{category.id}", category.name) for category in categories
        ) + "</p>"
    else:
        html = draft_html(token, parsed.amount, parsed.type, parsed.category, parsed.comment, parsed.items, family.currency if family else "ILS", receipt_discount=parsed.receipt_discount, can_retry_receipt=bool(receipt_file_id))
    result = await rich.send(bot, message.chat.id, html)
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(select(TransactionDraft).where(TransactionDraft.token == token))
        if draft:
            draft.message_id = result.get("message_id")


def draft_html(
    token: str,
    amount: Decimal,
    tx_type: str,
    category: str,
    comment: str | None,
    items=None,
    currency: str = "ILS",
    expanded_group: int | None = None,
    receipt_discount: Decimal = Decimal("0"),
    can_retry_receipt: bool = False,
    source_amount: Decimal | None = None,
    source_currency: str | None = None,
) -> str:
    direction = "Доход" if tx_type == "income" else "Расход"
    item_table = ""
    if items:
        groups = receipt_category_groups(items, receipt_discount)
        rows: list[str] = []
        for index, group in enumerate(groups):
            category_link = (
                link(f"tx:{token}:overview", group["category"])
                if expanded_group == index
                else link(f"txg:{token}:{index}", group["category"])
            )
            rows.append(
                f"<tr><td>{category_link}</td>"
                f"<td>{esc(money(group['amount'], currency))}</td><td>{group['count']}</td><td></td></tr>"
            )
            if expanded_group == index:
                for item_index, item in enumerate(items):
                    item_category = item.get("category") if isinstance(item, dict) else item.category
                    if item_category != group["category"]:
                        continue
                    item_name = item.get("name") if isinstance(item, dict) else item.name
                    item_amount = item.get("amount") if isinstance(item, dict) else item.amount
                    item_discount = Decimal(str(item.get("item_discount", 0) if isinstance(item, dict) else item.item_discount))
                    amount_text = money(item_amount, currency)
                    if item_discount > 0:
                        amount_text += f" (скидка −{money(item_discount, currency)})"
                    rows.append(
                        f"<tr><td>↳ {esc(item_name)}</td><td>{esc(amount_text)}</td><td></td>"
                        f"<td>{link(f'txi:{token}:{item_index}', 'Изменить категорию')}</td></tr>"
                    )
        group_rows = "".join(rows)
        item_table = (
            f"<h3>Будет создано операций: {len(groups)}</h3>"
            f"<table><tr><th>Категория / позиция</th><th>Сумма</th><th>Позиций</th><th></th></tr>{group_rows}</table>"
        )
    groups = receipt_category_groups(items or [], receipt_discount)
    save_label = f"✓ Записать {len(groups)} по категориям" if groups else "✓ Записать"
    category_row = "" if items else f"<tr><td>Категория</td><td>{link(f'tx:{token}:category', category)}</td></tr>"
    receipt_discount_line = (
        f"<p>Скидка на весь чек: −{esc(money(receipt_discount, currency))}; распределена между категориями пропорционально.</p>"
        if receipt_discount > 0
        else ""
    )
    return (
        "<h3>Проверить операцию</h3><table><tr><th>Поле</th><th>Значение</th></tr>"
        f"<tr><td>Сумма</td><td>{link(f'tx:{token}:amount', money(amount, currency))}</td></tr>"
        f"<tr><td>Валюта документа</td><td>{link(f'tx:{token}:currency', source_currency or currency)}</td></tr>"
        f"<tr><td>Тип</td><td>{link(f'tx:{token}:type', direction)}</td></tr>"
        f"{category_row}"
        f"<tr><td>Комментарий</td><td>{link(f'tx:{token}:comment', comment or '—')}</td></tr></table>"
        f"{receipt_discount_line}{item_table}<p>{link(f'tx:{token}:retry_receipt', 'Распознать заново') if can_retry_receipt else ''} {link(f'tx:{token}:save', save_label)} · {link(f'tx:{token}:cancel', 'Отмена')}</p>"
    )


def receipt_category_groups(items, receipt_discount: Decimal = Decimal("0")) -> list[dict[str, object]]:
    """Keep the receipt's order but aggregate all positions by category."""
    groups: OrderedDict[str, dict[str, object]] = OrderedDict()
    for item in items:
        category = item.get("category") if isinstance(item, dict) else item.category
        amount = Decimal(str(item.get("amount") if isinstance(item, dict) else item.amount))
        name = item.get("name") if isinstance(item, dict) else item.name
        group = groups.setdefault(category, {"category": category, "amount": Decimal("0"), "names": []})
        group["amount"] = Decimal(str(group["amount"])) + amount
        group["names"].append(name)
    result = [{**group, "count": len(group["names"])} for group in groups.values()]
    total = sum((Decimal(str(group["amount"])) for group in result), Decimal("0"))
    remaining_discount = Decimal(str(receipt_discount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    allocated = Decimal("0")
    for index, group in enumerate(result):
        if index == len(result) - 1:
            share = remaining_discount - allocated
        elif total > 0:
            share = (remaining_discount * Decimal(str(group["amount"])) / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            share = Decimal("0")
        allocated += share
        group["discount"] = share
        group["amount"] = Decimal(str(group["amount"])) - share
    return result


async def draft_html_for_family(session, draft: TransactionDraft, expanded_group: int | None = None) -> str:
    family = await session.get(Family, draft.family_id)
    return draft_html(
        draft.token,
        draft.amount,
        draft.type,
        draft.category,
        draft.comment,
        draft.items,
        family.currency if family else "ILS",
        expanded_group,
        draft.receipt_discount,
        bool(draft.receipt_file_id),
        draft.source_amount,
        draft.source_currency,
    )


async def active_draft(session, callback: CallbackQuery, token: str) -> TransactionDraft | None:
    draft = await session.scalar(
        select(TransactionDraft).where(TransactionDraft.token == token).with_for_update()
    )
    if (
        not draft
        or draft.status != "active"
        or draft.user_id != callback.from_user.id
        or draft.chat_id != callback.message.chat.id
        or draft.message_id != callback.message.message_id
        or draft.expires_at <= datetime.now(timezone.utc)
    ):
        return None
    return draft


@router.callback_query(F.data.startswith("tx:"))
async def transaction_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] not in {"save", "category", "amount", "currency", "type", "comment", "items", "overview", "retry_receipt", "cancel"}:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    _, token, action = parts
    notification_entries: list[tuple[Decimal, str, str, str | None]] = []
    notification_family_id: int | None = None
    notification_actor_id: int | None = None
    if action == "retry_receipt":
        async with SessionLocal() as session, session.begin():
            draft = await active_draft(session, callback, token)
            actor = await session.get(User, callback.from_user.id)
            if not draft or not draft.receipt_file_id or not can_manage_family(actor):
                await callback.answer("Исходный чек недоступен.", show_alert=True)
                return
            names = [item.name for item in await list_categories(session, draft.family_id)]
            runtime = await get_family_ai_settings(session, draft.family_id)
            file_id, mime_type = draft.receipt_file_id, draft.receipt_mime_type or "image/jpeg"
        await callback.answer("Повторно распознаю чек…")
        try:
            file = await bot.get_file(file_id)
            content = await bot.download_file(file.file_path)
            parsed = await parse_receipt_file(content.read(), mime_type, names, runtime)
        except (ReceiptError, AIUnavailableError) as error:
            await rich.send(bot, callback.message.chat.id, f"<p>{esc(error)}</p>")
            return
        if not parsed:
            await rich.send(bot, callback.message.chat.id, "<p>Не удалось повторно распознать чек. Черновик не изменён.</p>")
            return
        async with SessionLocal() as session, session.begin():
            draft = await active_draft(session, callback, token)
            if not draft:
                await rich.send(bot, callback.message.chat.id, "<p>Черновик устарел.</p>")
                return
            draft.amount, draft.type, draft.category, draft.comment = parsed.amount, parsed.type, parsed.category, parsed.comment
            draft.items, draft.receipt_discount = [item.model_dump(mode="json") for item in parsed.items] or None, parsed.receipt_discount
            draft.revision += 1
            html = await draft_html_for_family(session, draft)
        await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
        return
    async with SessionLocal() as session, session.begin():
        draft = await active_draft(session, callback, token)
        if not draft:
            await callback.answer("Запись устарела", show_alert=True)
            return
        actor = await session.get(User, callback.from_user.id)
        if not can_manage_family(actor):
            await callback.answer("Роль viewer может только просматривать данные.", show_alert=True)
            return
        if action == "save":
            items = draft.items or []
            notification_family_id, notification_actor_id = draft.family_id, draft.user_id
            item_total = sum((Decimal(str(item["amount"])) for item in items), Decimal("0")) - draft.receipt_discount
            if items and abs(item_total - draft.amount) > Decimal("0.01"):
                notification_family_id, notification_actor_id = None, None
                difference = draft.amount - item_total
                html = (
                    f"<p>Позиции чека дают {esc(money(item_total))}, а итог чека — {esc(money(draft.amount))}. "
                    f"Разница: {esc(money(difference))}. Черновик не сохранён: исправьте позиции или выберите «Распознать заново».</p>"
                )
            elif items:
                groups = receipt_category_groups(items, draft.receipt_discount)
                for group in groups:
                    category = await find_category(session, draft.family_id, str(group["category"]), draft.type) or await create_category(session, draft.family_id, str(group["category"]), draft.type)
                    names = ", ".join(str(name) for name in group["names"])
                    comment = f"Чек: {names}"[:2000]
                    await add_transaction(session, draft.family_id, draft.user_id, category, Decimal(str(group["amount"])), draft.type, comment, draft.receipt_file_id, draft.receipt_mime_type)
                    notification_entries.append((Decimal(str(group["amount"])), draft.type, category.name, comment))
                html = f"<p>Записано операций по категориям: {len(groups)}.</p>"
            else:
                category = await find_category(session, draft.family_id, draft.category, draft.type) or await create_category(session, draft.family_id, draft.category, draft.type)
                await add_transaction(session, draft.family_id, draft.user_id, category, draft.amount, draft.type, draft.comment, draft.receipt_file_id, draft.receipt_mime_type, source_amount=draft.source_amount, source_currency=draft.source_currency, exchange_rate=draft.exchange_rate, exchange_rate_date=draft.exchange_rate_date, exchange_rate_source=draft.exchange_rate_source)
                notification_entries.append((draft.amount, draft.type, category.name, draft.comment))
                html = "<p>Записано.</p>"
            if notification_family_id is not None:
                draft.status = "saved"
        elif action == "category":
            items = await list_categories(session, draft.family_id, draft.type)
            html = "<h3>Выберите категорию</h3><p>" + " · ".join(link(f"txc:{token}:{item.id}", item.name) for item in items) + "</p>"
        elif action == "currency":
            family = await session.get(Family, draft.family_id)
            choices = []
            for code in dict.fromkeys([family.currency if family else "ILS", "ILS", "USD", "EUR", "GBP", "RUB"]):
                choices.append(link(f"txcur:{token}:{code}", code))
            html = "<h3>Валюта документа</h3><p>Выберите валюту суммы на чеке.</p><p>" + " · ".join(choices) + f"</p><p>{link(f'tx:{token}:overview', '← К черновику')}</p>"
        elif action == "type":
            html = "<h3>Выберите тип</h3><p>" + link(f"txt:{token}:income", "Доход") + " · " + link(f"txt:{token}:expense", "Расход") + "</p>"
        elif action == "items":
            family = await session.get(Family, draft.family_id)
            item_rows = "".join(
                f"<tr><td>{esc(item['name'])}</td><td>{link(f'txi:{token}:{index}', item['category'])}</td><td>{esc(money(item['amount'], family.currency if family else 'ILS'))}</td></tr>"
                for index, item in enumerate(draft.items or [])
            )
            html = f"<h3>Позиции чека</h3><table><tr><th>Позиция</th><th>Категория</th><th>Сумма</th></tr>{item_rows}</table><p>Категория у позиции редактируется по ссылке. При сохранении позиции объединятся по категориям.</p><p>{link(f'tx:{token}:overview', '← К разбивке')} · {link(f'tx:{token}:save', '✓ Записать по категориям')} · {link(f'tx:{token}:cancel', 'Отмена')}</p>"
        elif action == "overview":
            html = await draft_html_for_family(session, draft)
        elif action in {"amount", "comment"}:
            await state.update_data(transaction_token=token, transaction_field=action)
            await state.set_state(TransactionEditFlow.waiting_value)
            prompt = "Введите сумму числом, например 500.50." if action == "amount" else "Введите комментарий к операции."
            await rich.send(bot, callback.message.chat.id, f"<p>{prompt}</p>")
            await callback.answer()
            return
        else: await session.delete(draft); html = "<p>Отменено.</p>"
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
    if action == "save" and notification_family_id is not None and notification_actor_id is not None:
        await notify_other_family_members(bot, notification_family_id, notification_actor_id, notification_entries)
        await send_current_month_report(
            bot,
            callback.message.chat.id,
            notification_actor_id,
            notification_family_id,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("txcur:"))
async def transaction_currency(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or len(parts[2]) != 3 or not parts[2].isalpha():
        await callback.answer("Некорректная валюта", show_alert=True)
        return
    token, source_currency = parts[1], parts[2].upper()
    async with SessionLocal() as session, session.begin():
        draft = await active_draft(session, callback, token)
        if not draft:
            await callback.answer("Черновик устарел", show_alert=True)
            return
        family = await session.get(Family, draft.family_id)
        source_amount = draft.source_amount or draft.amount
        try:
            amount, rate, rate_date = await convert_amount(source_amount, source_currency, family.currency if family else "ILS")
        except ReceiptError as error:
            await callback.answer(str(error), show_alert=True)
            return
        draft.amount, draft.source_amount, draft.source_currency = amount, source_amount, source_currency
        draft.exchange_rate, draft.exchange_rate_date, draft.exchange_rate_source = rate, rate_date, "Bank of Israel"
        draft.revision += 1
        html = await draft_html_for_family(session, draft)
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
    await callback.answer()


@router.callback_query(F.data.startswith("txi:"))
async def receipt_item_category_picker(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная позиция", show_alert=True); return
    token, index = parts[1], int(parts[2])
    async with SessionLocal() as session:
        draft = await active_draft(session, callback, token)
        if not draft or not draft.items or index >= len(draft.items):
            await callback.answer("Черновик устарел", show_alert=True); return
        categories = await list_categories(session, draft.family_id, draft.type)
    html = "<h3>Категория позиции</h3><p>" + " · ".join(link(f"txic:{token}:{index}:{category.id}", category.name) for category in categories) + "</p>"
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html); await callback.answer()


@router.callback_query(F.data.startswith("txg:"))
async def receipt_category_detail(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная категория", show_alert=True)
        return
    token, group_index = parts[1], int(parts[2])
    async with SessionLocal() as session:
        draft = await active_draft(session, callback, token)
        if not draft or not draft.items:
            await callback.answer("Черновик устарел", show_alert=True)
            return
        groups = receipt_category_groups(draft.items, draft.receipt_discount)
        if group_index >= len(groups):
            await callback.answer("Категория недоступна", show_alert=True)
            return
        html = await draft_html_for_family(session, draft, expanded_group=group_index)
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
    await callback.answer()


@router.callback_query(F.data.startswith("txic:"))
async def receipt_item_category_set(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await callback.answer("Некорректная категория", show_alert=True); return
    token, index, category_id = parts[1], int(parts[2]), int(parts[3])
    async with SessionLocal() as session, session.begin():
        draft = await active_draft(session, callback, token)
        category = await session.get(Category, category_id)
        if not draft or not draft.items or index >= len(draft.items) or not category or category.family_id != draft.family_id or category.type != draft.type:
            await callback.answer("Выбор устарел", show_alert=True); return
        draft.items[index]["category"] = category.name
        draft.revision += 1
        html = await draft_html_for_family(session, draft)
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html); await callback.answer()


@router.callback_query(F.data.startswith("txc:"))
async def transaction_category(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await callback.answer("Некорректная категория", show_alert=True)
        return
    _, token, category_id = parts
    async with SessionLocal() as session, session.begin():
        draft = await active_draft(session, callback, token)
        actor = await session.get(User, callback.from_user.id)
        category = await session.get(Category, int(category_id))
        if not can_manage_family(actor) or not draft or not category or category.family_id != draft.family_id or category.type != draft.type: await callback.answer("Выбор устарел", show_alert=True); return
        draft.category = category.name
        draft.revision += 1
        html = await draft_html_for_family(session, draft)
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html); await callback.answer()


@router.callback_query(F.data.startswith("txt:"))
async def transaction_type(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] not in {"income", "expense"}:
        await callback.answer("Некорректный тип", show_alert=True)
        return
    _, token, tx_type = parts
    async with SessionLocal() as session, session.begin():
        draft = await active_draft(session, callback, token)
        actor = await session.get(User, callback.from_user.id)
        if not draft or not can_manage_family(actor):
            await callback.answer("Запись устарела", show_alert=True)
            return
        draft.type = tx_type
        draft.revision += 1
        html = await draft_html_for_family(session, draft)
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, html)
    await callback.answer()


@router.message(TransactionEditFlow.waiting_value)
async def transaction_edit_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    saved_id, saved_field = data.get("saved_transaction_id"), data.get("saved_transaction_field")
    if saved_id and saved_field in {"amount", "date", "comment"}:
        async with SessionLocal() as session, session.begin():
            user = await session.get(User, message.from_user.id)
            tx = await session.get(Transaction, int(saved_id))
            if not user or not tx or tx.family_id != user.family_id or not can_manage_family(user):
                await state.clear()
                await rich.send(bot, message.chat.id, "<p>Операция недоступна.</p>")
                return
            if saved_field == "amount":
                try:
                    value = Decimal((message.text or "").replace(",", "."))
                    if value <= 0: raise InvalidOperation
                except InvalidOperation:
                    await rich.send(bot, message.chat.id, "<p>Введите положительную сумму, например <code>500.50</code>.</p>")
                    return
                await edit_transaction(session, tx, user.id, amount=value)
            elif saved_field == "date":
                try:
                    value = datetime.strptime((message.text or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError:
                    await rich.send(bot, message.chat.id, "<p>Используйте формат <code>2026-09-17 14:30</code>.</p>")
                    return
                await edit_transaction(session, tx, user.id, date=value)
            else:
                value = (message.text or "").strip()
                if len(value) > 2000:
                    await rich.send(bot, message.chat.id, "<p>Комментарий не должен быть длиннее 2000 символов.</p>")
                    return
                # Empty string is an explicit clear (None means "leave as is"
                # in the repository API), and it is recorded in the audit.
                await edit_transaction(session, tx, user.id, comment=value)
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Операция обновлена. Откройте /transactions, чтобы увидеть актуальный журнал.</p>")
        return
    token, field = data.get("transaction_token"), data.get("transaction_field")
    if not token or field not in {"amount", "comment"}:
        await state.clear()
        await rich.send(bot, message.chat.id, "<p>Сессия редактирования устарела.</p>")
        return
    async with SessionLocal() as session, session.begin():
        draft = await session.scalar(select(TransactionDraft).where(TransactionDraft.token == token).with_for_update())
        if not draft or draft.status != "active" or draft.user_id != message.from_user.id or draft.chat_id != message.chat.id or draft.expires_at <= datetime.now(timezone.utc):
            await state.clear()
            await rich.send(bot, message.chat.id, "<p>Черновик устарел.</p>")
            return
        if field == "amount":
            try:
                amount = Decimal((message.text or "").replace(",", "."))
                if amount <= 0:
                    raise InvalidOperation
            except InvalidOperation:
                await rich.send(bot, message.chat.id, "<p>Введите положительную сумму, например <code>500.50</code>.</p>")
                return
            draft.amount = amount
        else:
            comment = (message.text or "").strip()
            if len(comment) > 2000:
                await rich.send(bot, message.chat.id, "<p>Комментарий не должен быть длиннее 2000 символов.</p>")
                return
            draft.comment = comment or None
        draft.revision += 1
        html = await draft_html_for_family(session, draft)
        message_id = draft.message_id
    await state.clear()
    await rich.edit(bot, message.chat.id, message_id, html)
    await rich.send(bot, message.chat.id, "<p>Черновик обновлён.</p>")


@router.callback_query(F.data == "ui:report")
async def shortcut_report(callback: CallbackQuery, bot: Bot) -> None:
    user = await ensure_callback_user(callback)
    if not user.family_id:
        await rich.send(bot, callback.message.chat.id, "<p>Сначала выполните /start.</p>")
        await callback.answer()
        return
    screen = await create_report_screen(user, callback.message.chat.id, restore_view=True)
    await render_report(bot, screen)
    await callback.answer()


async def parse_receipt_file(
    content: bytes, mime_type: str, categories: list[str], runtime
) -> ParsedTransaction | None:
    if mime_type.lower() == "application/pdf":
        parsed = await ai_parser.parse_images(
            render_pdf_pages(content), categories, runtime, native_text=extract_pdf_text(content)
        )
    else:
        parsed = await ai_parser.parse_image(content, mime_type, categories, runtime)
    return reconcile_receipt_totals(parsed)


def receipt_attachment(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return message.photo[-1].file_id, "image/jpeg"
    if message.document and (
        (message.document.mime_type or "").startswith("image/")
        or (message.document.mime_type or "").lower() == "application/pdf"
        or (message.document.file_name or "").lower().endswith(".pdf")
    ):
        return message.document.file_id, (message.document.mime_type or mimetypes.guess_type(message.document.file_name or "")[0] or "image/jpeg")
    return None


async def recognize_receipt_messages(messages: list[Message], bot: Bot, one_receipt: bool) -> None:
    """Recognise one attachment per receipt, or several photos as one receipt."""
    if not messages:
        return
    user = await ensure_user(messages[0])
    if not user.family_id:
        await rich.send(bot, messages[0].chat.id, "<p>Сначала выполните /start.</p>")
        return
    attachments = [attachment for message in messages if (attachment := receipt_attachment(message))]
    if not attachments:
        await rich.send(bot, messages[0].chat.id, "<p>Пришлите изображение или PDF-чек.</p>")
        return
    async with SessionLocal() as session:
        names = [item.name for item in await list_categories(session, user.family_id)]
        runtime = await get_family_ai_settings(session, user.family_id)
    if not one_receipt:
        for message in messages:
            attachment = receipt_attachment(message)
            if attachment:
                await recognize_receipt_messages([message], bot, one_receipt=True)
        return
    await rich.send(bot, messages[0].chat.id, "<p>Распознаю чек…</p>")
    try:
        pages: list[tuple[bytes, str]] = []
        for file_id, mime_type in attachments:
            file = await bot.get_file(file_id)
            content = await bot.download_file(file.file_path)
            raw = content.read()
            if mime_type.lower() == "application/pdf":
                pages.extend(render_pdf_pages(raw))
            else:
                pages.append((raw, mime_type))
        parsed = await ai_parser.parse_images(pages, names, runtime)
        parsed = reconcile_receipt_totals(parsed)
    except (ReceiptError, AIUnavailableError) as error:
        await rich.send(bot, messages[0].chat.id, f"<p>{esc(error)}</p>")
        return
    if not parsed:
        await rich.send(bot, messages[0].chat.id, "<p>Не удалось распознать чек. Пришлите страницы ещё раз или введите операцию вручную.</p>")
        return
    file_id, mime_type = attachments[0]
    await ask_confirmation(messages[0], bot, parsed, receipt_file_id=file_id, receipt_mime_type=mime_type)


async def flush_receipt_album(key: str, bot: Bot) -> None:
    await asyncio.sleep(0.8)
    messages = _pending_receipt_albums.pop(key, [])
    if len(messages) <= 1:
        await recognize_receipt_messages(messages, bot, one_receipt=True)
        return
    messages.sort(key=lambda item: item.message_id)
    groups = await detect_receipt_groups(messages, bot)
    if groups:
        for group in groups:
            await recognize_receipt_messages(group, bot, one_receipt=True)
        return
    token = secrets.token_urlsafe(12)
    _receipt_album_choices[token] = ReceiptAlbum(
        user_id=messages[0].from_user.id, chat_id=messages[0].chat.id,
        messages=messages, expires_at=expiry(),
    )
    await rich.send(
        bot, messages[0].chat.id,
        "<h3>Несколько фото</h3><p>Что на них?</p><p>"
        + link(f"album:{token}:one", "Один длинный чек") + " · "
        + link(f"album:{token}:many", "Отдельные чеки") + "</p>",
    )


async def detect_receipt_groups(messages: list[Message], bot: Bot) -> list[list[Message]] | None:
    """Use a small, cheap vision call only to split an album into receipts."""
    user = await ensure_user(messages[0])
    if not user.family_id:
        return None
    async with SessionLocal() as session:
        runtime = await get_family_ai_settings(session, user.family_id)
    if not runtime.openai_api_key:
        return None
    try:
        content: list[dict] = [{"type": "text", "text": (
            "This is a Telegram album of receipt photos, numbered from 0. Group photos that are pages of the SAME physical receipt. "
            "Different receipts normally have their own merchant header and final total. A long receipt may continue across photos. "
            "Return uncertain=true if the boundary cannot be determined safely."
        )}]
        for index, message in enumerate(messages):
            attachment = receipt_attachment(message)
            if not attachment:
                return None
            file = await bot.get_file(attachment[0])
            raw = (await bot.download_file(file.file_path)).read()
            if attachment[1].lower() == "application/pdf":
                return None  # Albums with PDFs are rare; retain the explicit choice.
            content.append({"type": "text", "text": f"Photo {index}:"})
            content.append({"type": "image_url", "image_url": {"url": f"data:{attachment[1]};base64,{base64.b64encode(raw).decode('ascii')}", "detail": "low"}})
        client = AsyncOpenAI(api_key=runtime.openai_api_key)
        try:
            response = await client.chat.completions.create(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": content}],
                response_format={"type": "json_schema", "json_schema": {"name": "receipt_album_groups", "strict": True, "schema": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"uncertain": {"type": "boolean"}, "groups": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
                    "required": ["uncertain", "groups"],
                }}},
            )
        finally:
            await client.close()
        result = json.loads(response.choices[0].message.content or "{}")
        groups = result.get("groups")
        indexes = [index for group in groups for index in group] if isinstance(groups, list) else []
        if result.get("uncertain") or sorted(indexes) != list(range(len(messages))) or any(not group for group in groups):
            return None
        return [[messages[index] for index in group] for group in groups]
    except Exception:
        logging.getLogger(__name__).info("Could not classify receipt album boundaries", exc_info=True)
        return None


@router.message(F.photo | F.document)
async def receipt_media(message: Message, bot: Bot) -> None:
    """Recognize one file immediately or collect a Telegram media album."""
    if not message.media_group_id:
        await recognize_receipt_messages([message], bot, one_receipt=True)
        return
    key = f"{message.chat.id}:{message.media_group_id}"
    group = _pending_receipt_albums.setdefault(key, [])
    group.append(message)
    if len(group) == 1:
        asyncio.create_task(flush_receipt_album(key, bot))


@router.callback_query(F.data.startswith("album:"))
async def receipt_album_action(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] not in {"one", "many"}:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    album = _receipt_album_choices.pop(parts[1], None)
    if not album or album.user_id != callback.from_user.id or album.chat_id != callback.message.chat.id or album.expires_at <= datetime.now(timezone.utc):
        await callback.answer("Выбор устарел. Отправьте фото ещё раз.", show_alert=True)
        return
    await rich.edit(bot, callback.message.chat.id, callback.message.message_id, "<p>Принято. Распознаю…</p>")
    await callback.answer()
    await recognize_receipt_messages(album.messages, bot, one_receipt=parts[2] == "one")


@router.message(F.text.func(lambda value: bool(ENABLE_RECEIPT_URLS and value and is_http_url(value.strip()))))
async def receipt_url(message: Message, bot: Bot) -> None:
    """Treat a standalone public URL as a receipt, not a manual operation."""
    raw_url = (message.text or "").strip()
    user = await ensure_user(message)
    if not user.family_id:
        await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>")
        return
    await rich.send(bot, message.chat.id, "<p>Загружаю и распознаю чек по ссылке…</p>")
    try:
        payload = await fetch_receipt_url(raw_url)
        async with SessionLocal() as session:
            names = [item.name for item in await list_categories(session, user.family_id)]
            runtime = await get_family_ai_settings(session, user.family_id)
        if payload.kind == "text":
            parsed = await ai_parser.parse_text(f"Содержимое чека: {payload.content}", names, runtime)
        else:
            parsed = await parse_receipt_file(payload.content, payload.mime_type or "image/jpeg", names, runtime)
    except (ReceiptError, AIUnavailableError) as error:
        await rich.send(bot, message.chat.id, f"<p>{esc(error)}</p>")
        return
    except Exception:
        logging.exception("Receipt URL recognition failed")
        await rich.send(bot, message.chat.id, "<p>Не удалось загрузить чек по ссылке. Пришлите скриншот или PDF.</p>")
        return
    if not parsed:
        await rich.send(bot, message.chat.id, "<p>Не удалось распознать чек по ссылке. Пришлите скриншот или PDF.</p>")
        return
    await ask_confirmation(message, bot, parsed)


@router.message(AdvancedFlow.waiting_value)
async def advanced_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    mode, raw = data.get("advanced_mode"), (message.text or "").strip()
    user = await ensure_user(message)
    if mode not in {"account", "transfer", "recurring", "split"} or not can_manage_family(user):
        await state.clear(); await rich.send(bot, message.chat.id, "<p>Сессия недоступна.</p>"); return
    try:
        async with SessionLocal() as session, session.begin():
            if mode == "account":
                name, currency = [part.strip() for part in raw.split(";", 1)]
                account = await create_account(session, user.family_id, name, currency)
                result = f"Счёт «{esc(account.name)}» создан."
            elif mode == "transfer":
                source, target, amount_text, *comment = [part.strip() for part in raw.split(";")]
                accounts = list(await session.scalars(select(Account).where(Account.family_id == user.family_id, Account.is_active.is_(True))))
                from_account = next((item for item in accounts if item.name.casefold() == source.casefold()), None)
                to_account = next((item for item in accounts if item.name.casefold() == target.casefold()), None)
                if not from_account or not to_account: raise ValueError("Укажите существующие активные счета.")
                transfer = await create_transfer(session, user.family_id, user.id, from_account, to_account, Decimal(amount_text.replace(",", ".")), comment="; ".join(comment) or None)
                result = f"Перевод №{transfer.id} создан."
            elif mode == "recurring":
                amount_text, category_name, cadence, *comment = [part.strip() for part in raw.split(";")]
                category = await find_category(session, user.family_id, category_name, "expense")
                if not category: raise ValueError("Для регулярной операции выберите активную расходную категорию.")
                recurring = await create_recurring_transaction(session, user.family_id, user.id, category, Decimal(amount_text.replace(",", ".")), "expense", cadence, datetime.now(timezone.utc), comment="; ".join(comment) or None)
                result = f"Регулярная операция №{recurring.id} создана."
            else:
                parts = [part.strip() for part in raw.split(";") if part.strip()]
                if len(parts) < 2: raise ValueError("Нужны минимум две позиции: Категория=сумма; Категория=сумма.")
                lines = []
                for part in parts:
                    if "=" not in part: raise ValueError("Каждая позиция имеет вид Категория=сумма.")
                    name, amount_text = [value.strip() for value in part.split("=", 1)]
                    category = await find_category(session, user.family_id, name, "expense")
                    if not category: raise ValueError(f"Категория «{name}» не найдена.")
                    lines.append((category, Decimal(amount_text.replace(",", ".")), None))
                group, transactions = await create_split_payment(session, user.family_id, user.id, "expense", lines)
                result = f"Split-платёж №{group.id}: создано операций {len(transactions)}."
    except (ValueError, InvalidOperation) as error:
        await rich.send(bot, message.chat.id, f"<p>{esc(error)}</p>")
        return
    await state.clear()
    await rich.send(bot, message.chat.id, f"<p>{result}</p>")


@router.message(F.text & ~F.text.startswith("/"))
async def text(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id: await rich.send(bot, message.chat.id, "<p>Сначала выполните /start.</p>"); return
    async with SessionLocal() as session:
        names = [item.name for item in await list_categories(session, user.family_id)]
        runtime = await get_family_ai_settings(session, user.family_id)
    try:
        parsed = await ai_parser.parse_text(message.text or "", names, runtime)
    except AIUnavailableError as error:
        await rich.send(bot, message.chat.id, f"<p>{esc(error)}</p>")
        return
    if parsed:
        amount_only = bool(re.fullmatch(r"\+?\d+(?:[.,]\d{1,2})?", (message.text or "").strip()))
        await ask_confirmation(message, bot, parsed, choose_category=amount_only)
    else: await rich.send(bot, message.chat.id, "<p>Не удалось распознать операцию. Пример: <code>500 Продукты молоко</code>.</p>")


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(); dp.include_router(router); return dp
