"""Raw Telegram rich-message transport and safe HTML/link helpers.

aiogram does not model Bot API's experimental rich endpoints yet, so this small
adapter deliberately calls the raw endpoint while keeping all UI rendering out
of handlers.
"""
from __future__ import annotations

import html
import json
from typing import Any

from aiohttp import FormData

from aiogram import Bot


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def link(action: str, label: object) -> str:
    """A rich-text action, intentionally not an InlineKeyboard button."""
    return f'<tg-button type="callback_data" style="link" data="{esc(action)}">{esc(label)}</tg-button>'


class RichMessageService:
    async def _call(
        self, bot: Bot, method: str, payload: dict[str, Any], files: dict[str, bytes] | None = None
    ) -> dict[str, Any]:
        session = await bot.session.create_session()
        url = bot.session.api.api_url(token=bot.token, method=method)
        body_payload: Any = payload
        if files:
            form = FormData()
            for key, value in payload.items():
                form.add_field(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value))
            for name, content in files.items():
                form.add_field(name, content, filename=f"{name}.png", content_type="image/png")
            body_payload = form
        async with session.post(url, json=None if files else payload, data=body_payload if files else None) as response:
            body = await response.json(content_type=None)
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description', body)}")
        return body.get("result", {})

    async def send(
        self, bot: Bot, chat_id: int, html: str, chart_png: bytes | None = None
    ) -> dict[str, Any]:
        rich_message: dict[str, Any] = {"html": html}
        files = None
        if chart_png:
            rich_message["media"] = [
                {"id": "report_chart", "media": {"type": "photo", "media": "attach://report_chart"}}
            ]
            files = {"report_chart": chart_png}
        return await self._call(bot, "sendRichMessage", {"chat_id": chat_id, "rich_message": rich_message}, files)

    async def edit(
        self, bot: Bot, chat_id: int, message_id: int, html: str, chart_png: bytes | None = None
    ) -> dict[str, Any]:
        rich_message: dict[str, Any] = {"html": html}
        files = None
        if chart_png:
            rich_message["media"] = [
                {"id": "report_chart", "media": {"type": "photo", "media": "attach://report_chart"}}
            ]
            files = {"report_chart": chart_png}
        try:
            return await self._call(
                bot,
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "rich_message": rich_message},
                files,
            )
        except RuntimeError as error:
            # Telegram cancels an older edit when a later rich-link click
            # already updates the same message. The later request is the UI
            # state the user asked for, so surfacing this as a handler crash is
            # both noisy and misleading.
            if any(
                marker in str(error).lower()
                for marker in ("canceled by new edit message request", "message is not modified")
            ):
                return {}
            raise
