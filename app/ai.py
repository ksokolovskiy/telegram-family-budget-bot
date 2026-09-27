from __future__ import annotations

import base64
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from openai import AsyncOpenAI, RateLimitError
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.settings_store import FamilyAISettings

logger = logging.getLogger(__name__)


class AIUnavailableError(RuntimeError):
    """A user-actionable provider failure, safe to show in Telegram."""

SYSTEM_PROMPT = """Вы — строго структурированный модуль классификации расходов для финансового Telegram-бота.
Вам на вход поступает текстовое сообщение от пользователя или изображение (чек/скриншот), а также список существующих категорий этой семьи: {categories_list}.

Сначала извлеките факты, затем классифицируйте. Сумма amount — только напечатанный
итог к оплате (TOTAL / סה״כ / לתשלום), не subtotal и не сумма скидки. Каждая
товарная строка чека должна быть отдельным items: сохраняйте напечатанную сумму
строки, не объединяйте одинаковые товары и не включайте в items subtotal, НДС,
способ оплаты, сдачу, итог или строку скидки.

Отрицательная строка рядом с товаром или строка акции — это важный факт, а не
строка, которую можно пропустить. Свяжите её с товаром/товарами, к которым она
относится. Для item укажите amount после такой скидки, а item_discount — полную
сумму скидки этого товара. Если скидка распространяется на несколько явно
указанных товаров, распределите её между ними без потери копеек. Если скидка
напечатана на ВЕСЬ чек и её нельзя отнести к товарам, укажите её в
receipt_discount. При этом сумма всех items минус receipt_discount обязана
равняться напечатанному итогу с точностью до одной агоры. Не придумывайте скидку:
используйте только отрицательные строки и условия акций, напечатанные на чеке.

Если приложено несколько изображений, они могут быть перекрывающимися частями
одного длинного чека. Рассматривайте их как последовательность страниц: строка,
видимая в перекрытии на двух фото, является одним товаром и должна попасть в
items ровно один раз.

Категория каждого товара должна быть ровно одной из переданных категорий. Еда,
напитки и продукты всегда «Продукты», если такая категория есть, даже в чеке из
магазина товаров для дома. category верхнего уровня — категория с наибольшей
суммой позиций; для чека с items она не влияет на сохранение.

Вы обязаны вернуть ответ строго в формате JSON без какого-либо дополнительного текста или разметки markdown (markdown blocks):
{
  "amount": float,
  "type": "income" | "expense",
  "category": "название_категории",
  "comment": "очищенный текст комментария или null",
  "receipt_discount": float,
  "items": [{"name": "позиция чека", "amount": float, "item_discount": float, "category": "категория"}]
}

Для изображения чека обязательно верните items: строго одну запись на каждую
реальную строку/товар чека с положительной ценой. Никогда не объединяйте товары
по категории и не создавайте сводные строки категорий. amount — цена позиции после её собственной скидки, но до скидки на весь
чек. item_discount — скидка, относящаяся только к этой позиции (или 0).
receipt_discount — только общая скидка на весь чек (или 0); не включайте её в
amount позиций. Для обычного текста верните пустой массив и receipt_discount 0.
Особенно тщательно отличайте продукты питания и напитки от товаров для дома:
если в категориях семьи есть «Продукты», отнесите туда еду, напитки и продукты
питания, даже если остальные позиции того же чека относятся к «Дом».
"""


class ReceiptItem(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    amount: Decimal = Field(gt=0)
    item_discount: Decimal = Field(default=Decimal("0"), ge=0)
    category: str = Field(min_length=1, max_length=120)


class ParsedTransaction(BaseModel):
    amount: Decimal = Field(gt=0)
    type: str
    category: str = Field(min_length=1)
    comment: str | None = None
    items: list[ReceiptItem] = Field(default_factory=list)
    receipt_discount: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in {"income", "expense"}:
            raise ValueError("type must be income or expense")
        return value


def receipt_response_format(categories: list[str]) -> dict[str, Any]:
    """Strict provider schema: JSON mode alone only promises valid JSON."""
    choices = list(dict.fromkeys(categories)) or ["Другое"]
    item = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string"}, "amount": {"type": "number"},
            "item_discount": {"type": "number"}, "category": {"type": "string", "enum": choices},
        },
        "required": ["name", "amount", "item_discount", "category"],
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "receipt_extraction", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "amount": {"type": "number"}, "type": {"type": "string", "enum": ["income", "expense"]},
                    "category": {"type": "string", "enum": choices}, "comment": {"type": ["string", "null"]},
                    "receipt_discount": {"type": "number"}, "items": {"type": "array", "items": item},
                },
                "required": ["amount", "type", "category", "comment", "receipt_discount", "items"],
            },
        },
    }


def reconcile_receipt_totals(parsed: ParsedTransaction | None) -> ParsedTransaction | None:
    """Make small OCR rounding omissions explicit without changing the receipt total.

    A receipt's printed total is authoritative.  Vision models occasionally
    omit a tiny line (most often an IKEA rounding/adjustment line) even though
    they read the total correctly.  Do not reject an otherwise usable receipt:
    preserve the discrepancy as a visible, separately named line in the
    largest category.  Larger discrepancies still require a re-scan/manual
    correction, because they are likely a missing product rather than rounding.
    """
    if not parsed or not parsed.items:
        return parsed
    items_total = sum((item.amount for item in parsed.items), Decimal("0"))
    # Some vision responses provide a receipt discount while their line amounts
    # already add up to the final total.  Counting it again would deduct it twice.
    if parsed.receipt_discount > 0 and abs(items_total - parsed.amount) <= Decimal("0.01"):
        return parsed.model_copy(update={"receipt_discount": Decimal("0")})

    residual = (parsed.amount - (items_total - parsed.receipt_discount)).quantize(Decimal("0.01"))
    if residual == 0 or abs(residual) > Decimal("2.00"):
        return parsed

    # When the rows exceed the total, the residual is a receipt-wide discount
    # not represented by the model.  When they fall short, represent the
    # printed adjustment openly, instead of silently changing a product price.
    if residual < 0:
        return parsed.model_copy(update={"receipt_discount": parsed.receipt_discount - residual})
    largest = max(parsed.items, key=lambda item: item.amount)
    adjustment = ReceiptItem(
        name="Корректировка по итогу чека",
        amount=residual,
        category=largest.category,
    )
    return parsed.model_copy(update={"items": [*parsed.items, adjustment]})


def receipt_totals_match(parsed: ParsedTransaction | None) -> bool:
    """Whether extracted lines reconcile exactly with the printed total."""
    if not parsed or not parsed.items:
        return True
    item_total = sum((item.amount for item in parsed.items), Decimal("0")) - parsed.receipt_discount
    return abs(item_total - parsed.amount) <= Decimal("0.01")


class AIParser:
    def __init__(self) -> None:
        self.client: AsyncOpenAI | None = None
        self._api_key: str | None = None

    def _client_for(self, runtime: FamilyAISettings) -> AsyncOpenAI | None:
        """Recreate only when a family owner changes that family's key."""
        if not runtime.openai_api_key:
            self.client = None
            self._api_key = None
            return None
        if self.client is None or self._api_key != runtime.openai_api_key:
            self.client = AsyncOpenAI(api_key=runtime.openai_api_key)
            self._api_key = runtime.openai_api_key
        return self.client

    async def parse_text(self, text: str, categories: list[str], runtime: FamilyAISettings) -> ParsedTransaction | None:
        if not self._client_for(runtime):
            return fallback_parse_text(text, categories)
        return await self._parse([{"type": "text", "text": text}], categories, runtime)

    async def parse_image(self, image_bytes: bytes, mime_type: str, categories: list[str], runtime: FamilyAISettings) -> ParsedTransaction | None:
        return await self.parse_images([(image_bytes, mime_type)], categories, runtime)

    async def parse_images(self, images: list[tuple[bytes, str]], categories: list[str], runtime: FamilyAISettings, native_text: str | None = None) -> ParsedTransaction | None:
        if not self._client_for(runtime):
            return None
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": "Распознай чек. Верни одну отдельную позицию items для каждой строки товара на чеке, даже если категории совпадают; не суммируй и не группируй товары.",
        }]
        if native_text:
            content.append({"type": "text", "text": "Ниже точный текстовый слой PDF. Используй его как основной источник названий и сумм; изображение — для проверки расположения и отсутствующих символов.\n\n" + native_text})
        for image_bytes, mime_type in images:
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
            content.append({"type": "image_url", "image_url": {"url": data_url, "detail": "high"}})
        return await self._parse(
            content,
            categories, runtime,
        )

    async def parse_receipt_images(
        self,
        images: list[tuple[bytes, str]],
        categories: list[str],
        runtime: FamilyAISettings,
        native_text: str | None = None,
    ) -> ParsedTransaction | None:
        """Extract a receipt and make one focused repair attempt if it does not add up.

        A meaningful mismatch is usually a missed discount, promotion or an
        overlap between receipt photos. It is safer to inspect those facts once
        more than to show an unusable draft or invent a balancing adjustment.
        """
        parsed = reconcile_receipt_totals(
            await self.parse_images(images, categories, runtime, native_text=native_text)
        )
        if receipt_totals_match(parsed):
            return parsed

        assert parsed is not None  # guarded by receipt_totals_match above
        original = parsed.model_dump(mode="json")
        item_total = sum((item.amount for item in parsed.items), Decimal("0"))
        repair_content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                "Проверьте распознавание чека ещё раз. Предыдущий черновик не "
                "сошёлся: сумма позиций за вычетом скидки не равна напечатанному "
                "итогу. Найдите пропущенные или неверно привязанные отрицательные "
                "строки скидок/акций и возможные задвоенные строки в перекрытии "
                "фото. Верните ПОЛНОСТЬЮ исправленный JSON по исходным фото, а не "
                "объяснение. Не добавляйте выдуманную корректировку.\n\n"
                f"Предыдущий черновик: {json.dumps(original, ensure_ascii=False)}\n"
                f"Сумма позиций минус скидка: {item_total - parsed.receipt_discount}; "
                f"напечатанный итог: {parsed.amount}."
            ),
        }]
        if native_text:
            repair_content.append({"type": "text", "text": "Текстовый слой PDF:\n" + native_text})
        for image_bytes, mime_type in images:
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
            repair_content.append({"type": "image_url", "image_url": {"url": data_url, "detail": "high"}})
        repaired = reconcile_receipt_totals(await self._parse(repair_content, categories, runtime))
        # A provider failure on the repair pass must not discard the original:
        # it remains a safe, non-actionable draft with a retry action.
        return repaired or parsed

    async def _parse(self, content: list[dict[str, Any]], categories: list[str], runtime: FamilyAISettings) -> ParsedTransaction | None:
        # Keep a local reference. Another family's simultaneous request may
        # replace the cache for later calls, but can never change the client
        # (and therefore API key) used by this request.
        client = self._client_for(runtime)
        if client is None:
            return None
        try:
            response = await client.chat.completions.create(
                model=runtime.openai_model,
                messages=[
                    # The example JSON in the prompt contains braces, so use
                    # a literal placeholder replacement instead of str.format.
                    {"role": "system", "content": SYSTEM_PROMPT.replace("{categories_list}", ", ".join(categories))},
                    {"role": "user", "content": content},
                ],
                response_format=receipt_response_format(categories),
            )
            raw = response.choices[0].message.content or "{}"
            return ParsedTransaction.model_validate(json.loads(raw))
        except RateLimitError as error:
            logger.warning("OpenAI request was rate-limited or has no credit: %s", error)
            raise AIUnavailableError("У OpenAI API key семьи закончился баланс. Пополните API billing и повторите.") from error
        except (json.JSONDecodeError, ValidationError) as error:
            logger.warning("OpenAI returned an invalid receipt payload: %s", error)
            return None
        except Exception:
            # Do not log receipt/image content or credentials; the exception
            # itself is needed to distinguish model/API failures from OCR.
            logger.exception("OpenAI receipt recognition request failed")
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
    # A recognised category is structured data, not part of the note.  Thus
    # "400 Авто заправка" becomes category "Авто", comment "заправка".
    comment = re.sub(re.escape(category), "", tail, count=1, flags=re.IGNORECASE).strip(" ,;:-") or None
    return ParsedTransaction(amount=amount, type=tx_type, category=category, comment=comment)
