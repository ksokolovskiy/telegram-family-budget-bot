from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import Category


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Создать семью", callback_data="family:create")],
            [InlineKeyboardButton(text="Ввести код приглашения", callback_data="family:join")],
        ]
    )


def confirm_transaction_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data=f"tx:save:{token}")],
            [InlineKeyboardButton(text="Изменить категорию", callback_data=f"tx:change:{token}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"tx:cancel:{token}")],
        ]
    )


def categories_keyboard(categories: list[Category], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=cat.name, callback_data=f"{prefix}:{cat.id}")] for cat in categories]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[InlineKeyboardButton(text="Нет категорий", callback_data="noop")]])


def report_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Текущий месяц", callback_data="report:month"),
                InlineKeyboardButton(text="Квартал", callback_data="report:quarter"),
                InlineKeyboardButton(text="Год", callback_data="report:year"),
            ]
        ]
    )
