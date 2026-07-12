from __future__ import annotations

import base64
import json
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import settings

SYSTEM_PROMPT = """Вы — строго структурированный модуль классификации расходов для финансового Telegram-бота.
Вам на вход поступает текстовое сообщение от пользователя или изображение (чек/скриншот), а также список существующих категорий этой семьи: {categories_list}.

Выделите сумму, определите тип транзакции (доход или расход), подберите наиболее подходящую категорию из списка (или предложите точное имя для новой, если совпадений нет) и сформулируйте краткий комментарий.

Вы обязаны вернуть ответ строго в формате JSON без какого-либо дополнительного текста или разметки markdown (markdown blocks):
{
  "amount": float,
  "type": "income" | "expense",
  "category": "название_категории",
  "comment": "очищенный текст комментария или null"
}
"""


class ParsedTransaction(BaseModel):
    amount: Decimal = Field(gt=0)
    type: str
    category: str = Field(min_length=1)
    comment: str | None = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in {"income", "expense"}:
            raise ValueError("type must be income or expense")
        return value


class AIParser:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    async def parse_text(self, text: str, categories: list[str]) -> ParsedTransaction | None:
        if not self.client:
            return fallback_parse_text(text, categories)
        return await self._parse([{"type": "text", "text": text}], categories)

    async def parse_image(self, image_bytes: bytes, mime_type: str, categories: list[str]) -> ParsedTransaction | None:
        if not self.client:
            return None
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        return await self._parse(
            [
                {"type": "text", "text": "Распознай чек или скриншот расхода."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
            categories,
        )

    async def _parse(self, content: list[dict[str, Any]], categories: list[str]) -> ParsedTransaction | None:
        try:
            response = await self.client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT.format(categories_list=", ".join(categories))},
                    {"role": "user", "content": content},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = response.choices[0].message.content or "{}"
            return ParsedTransaction.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError, Exception):
            return None


def fallback_parse_text(text: str, categories: list[str]) -> ParsedTransaction | None:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    raw_amount = parts[0].replace(",", ".")
    tx_type = "income" if raw_amount.startswith("+") else "expense"
    raw_amount = raw_amount.lstrip("+")
    try:
        amount = Decimal(raw_amount)
    except InvalidOperation:
        return None
    tail = parts[1] if len(parts) > 1 else "Другое"
    category = next((name for name in categories if name.lower() in tail.lower()), None)
    if not category:
        category = tail.split(maxsplit=1)[0].title() if tail else "Другое"
    comment = tail if tail and tail.lower() != category.lower() else None
    return ParsedTransaction(amount=amount, type=tx_type, category=category, comment=comment)
