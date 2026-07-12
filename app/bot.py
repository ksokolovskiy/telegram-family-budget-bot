from __future__ import annotations

import logging
import mimetypes
import secrets
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, Message

from app.ai import AIParser, ParsedTransaction
from app.db import SessionLocal
from app.keyboards import categories_keyboard, confirm_transaction_keyboard, report_keyboard, start_keyboard
from app.models import User
from app.repositories import (
    add_transaction,
    create_category,
    create_family_for_user,
    find_category,
    get_or_create_user,
    join_family,
    list_categories,
    upsert_budget,
)
from app.reports import build_report

logger = logging.getLogger(__name__)
router = Router()
ai_parser = AIParser()
pending_transactions: dict[str, ParsedTransaction] = {}


class JoinFamily(StatesGroup):
    waiting_code = State()


class BudgetFlow(StatesGroup):
    waiting_category = State()
    waiting_amount = State()


async def setup_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Инициализация бота"),
            BotCommand(command="budget", description="Плановые лимиты по категориям"),
            BotCommand(command="report", description="Отчет план-факт"),
            BotCommand(command="categories", description="Категории доходов/расходов"),
            BotCommand(command="help", description="Инструкция и примеры"),
        ]
    )


async def ensure_user(message: Message) -> User:
    async with SessionLocal() as session, session.begin():
        return await get_or_create_user(session, message.from_user.id, message.from_user.username)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user = await ensure_user(message)
    if user.family_id:
        await message.answer("Вы уже подключены к семейному бюджету. Можно вводить траты: `500 Продукты молоко`.")
        return
    await message.answer("Создайте семейный бюджет или подключитесь по коду приглашения.", reply_markup=start_keyboard())


@router.callback_query(F.data == "family:create")
async def cb_create_family(callback: CallbackQuery) -> None:
    async with SessionLocal() as session, session.begin():
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
        family = await create_family_for_user(session, user, f"Семья {callback.from_user.first_name or callback.from_user.id}")
    await callback.message.answer(f"Готово. Код приглашения: `{family.invite_code}`")
    await callback.answer()


@router.callback_query(F.data == "family:join")
async def cb_join_family(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(JoinFamily.waiting_code)
    await callback.message.answer("Введите код приглашения.")
    await callback.answer()


@router.message(JoinFamily.waiting_code)
async def join_by_code(message: Message, state: FSMContext) -> None:
    async with SessionLocal() as session, session.begin():
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        family = await join_family(session, user, message.text or "")
    await state.clear()
    if not family:
        await message.answer("Код не найден. Проверьте и попробуйте еще раз.")
        return
    await message.answer(f"Вы подключены к бюджету: {family.name}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Примеры ввода:\n"
        "`500 Продукты молоко и хлеб` — расход.\n"
        "`+150000 Зарплата` — доход.\n"
        "Можно прислать фото чека, бот распознает сумму и категорию."
    )


@router.message(Command("categories"))
async def cmd_categories(message: Message) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await message.answer("Сначала выполните /start и создайте или подключите семью.")
        return
    async with SessionLocal() as session:
        categories = await list_categories(session, user.family_id)
    lines = ["*Категории:*", *[f"• {cat.name} ({'доход' if cat.type == 'income' else 'расход'})" for cat in categories]]
    await message.answer("\n".join(lines))


@router.message(Command("budget"))
async def cmd_budget(message: Message, state: FSMContext) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await message.answer("Сначала выполните /start.")
        return
    async with SessionLocal() as session:
        categories = await list_categories(session, user.family_id, "expense")
    await state.set_state(BudgetFlow.waiting_category)
    await message.answer("Выберите категорию для лимита на текущий месяц.", reply_markup=categories_keyboard(categories, "budgetcat"))


@router.callback_query(BudgetFlow.waiting_category, F.data.startswith("budgetcat:"))
async def budget_category(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = int(callback.data.split(":", 1)[1])
    await state.update_data(category_id=category_id)
    await state.set_state(BudgetFlow.waiting_amount)
    await callback.message.answer("Введите лимит числом.")
    await callback.answer()


@router.message(BudgetFlow.waiting_amount)
async def budget_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal((message.text or "").replace(",", "."))
    except InvalidOperation:
        await message.answer("Введите сумму числом, например `30000`.")
        return
    data = await state.get_data()
    user = await ensure_user(message)
    async with SessionLocal() as session, session.begin():
        await upsert_budget(session, user.family_id, data["category_id"], amount)
    await state.clear()
    await message.answer("Лимит сохранен.")


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    await send_report(message, "month")


@router.callback_query(F.data.startswith("report:"))
async def cb_report(callback: CallbackQuery) -> None:
    kind = callback.data.split(":", 1)[1]
    async with SessionLocal() as session:
        user = await session.get(User, callback.from_user.id)
        if not user or not user.family_id:
            await callback.message.answer("Сначала выполните /start.")
        else:
            await callback.message.edit_text(await build_report(session, user.family_id, kind), reply_markup=report_keyboard())
    await callback.answer()


async def send_report(message: Message, kind: str) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await message.answer("Сначала выполните /start.")
        return
    async with SessionLocal() as session:
        await message.answer(await build_report(session, user.family_id, kind), reply_markup=report_keyboard())


@router.message(F.photo | F.document)
async def handle_media(message: Message, bot: Bot) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await message.answer("Сначала выполните /start.")
        return
    file_id = None
    mime_type = "image/jpeg"
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
        mime_type = message.document.mime_type or mimetypes.guess_type(message.document.file_name or "")[0] or "image/jpeg"
    if not file_id:
        await message.answer("Пришлите изображение чека или скриншота.")
        return
    file = await bot.get_file(file_id)
    data = await bot.download_file(file.file_path)
    image_bytes = data.read()
    async with SessionLocal() as session:
        categories = [cat.name for cat in await list_categories(session, user.family_id)]
    parsed = await ai_parser.parse_image(image_bytes, mime_type, categories)
    if not parsed:
        await message.answer("Не удалось автоматически распознать трату. Введите сумму вручную или выберите категорию")
        return
    await ask_confirmation(message, parsed)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    user = await ensure_user(message)
    if not user.family_id:
        await message.answer("Сначала выполните /start.")
        return
    async with SessionLocal() as session:
        categories = [cat.name for cat in await list_categories(session, user.family_id)]
    parsed = await ai_parser.parse_text(message.text or "", categories)
    if not parsed:
        await message.answer("Не удалось автоматически распознать трату. Введите сумму вручную или выберите категорию")
        return
    await ask_confirmation(message, parsed)


async def ask_confirmation(message: Message, parsed: ParsedTransaction) -> None:
    token = secrets.token_urlsafe(8)
    pending_transactions[token] = parsed
    direction = "Доход" if parsed.type == "income" else "Расход"
    await message.answer(
        f"Записать {direction}: {parsed.amount} ₽ в категорию \"{parsed.category}\"?",
        reply_markup=confirm_transaction_keyboard(token),
    )


@router.callback_query(F.data.startswith("tx:save:"))
async def save_transaction(callback: CallbackQuery) -> None:
    token = callback.data.rsplit(":", 1)[1]
    parsed = pending_transactions.pop(token, None)
    if not parsed:
        await callback.answer("Запись устарела", show_alert=True)
        return
    async with SessionLocal() as session, session.begin():
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
        category = await find_category(session, user.family_id, parsed.category, parsed.type)
        if not category:
            category = await create_category(session, user.family_id, parsed.category, parsed.type)
        await add_transaction(session, user.family_id, user.id, category, parsed.amount, parsed.type, parsed.comment)
    await callback.message.edit_text("Записано.")
    await callback.answer()


@router.callback_query(F.data.startswith("tx:cancel:"))
async def cancel_transaction(callback: CallbackQuery) -> None:
    token = callback.data.rsplit(":", 1)[1]
    pending_transactions.pop(token, None)
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
