from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import rich_bot


@pytest.mark.parametrize('delete_fails', [False, True])
async def test_comment_refreshes_draft_and_removes_input_messages(monkeypatch, delete_fails):
    draft = SimpleNamespace(token='draft', status='active', user_id=1, chat_id=2,
                            expires_at=datetime.now(timezone.utc)+timedelta(hours=1),
                            comment='old', revision=1, message_id=10)
    session = SimpleNamespace(scalar=AsyncMock(return_value=draft))
    @asynccontextmanager
    async def context():
        yield session
    session.begin = context
    monkeypatch.setattr(rich_bot, 'SessionLocal', context)
    render = AsyncMock(side_effect=lambda session, draft: f'<p>{draft.comment}</p><p>✓ Записать</p>')
    monkeypatch.setattr(rich_bot, 'draft_html_for_family', render)
    rich = SimpleNamespace(edit=AsyncMock(), send=AsyncMock())
    monkeypatch.setattr(rich_bot, 'rich', rich)
    state = SimpleNamespace(get_data=AsyncMock(return_value={
        'transaction_token':'draft', 'transaction_field':'comment', 'transaction_edit_prompt_id':11,
    }), clear=AsyncMock())
    message = SimpleNamespace(text='новый комментарий', message_id=12,
                              from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=2))
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=RuntimeError if delete_fails else None))
    await rich_bot.transaction_edit_value(message, state, bot)
    assert draft.comment == 'новый комментарий'
    assert draft.revision == 2
    rich.edit.assert_awaited_once_with(bot, 2, 10, '<p>новый комментарий</p><p>✓ Записать</p>')
    rich.send.assert_not_awaited()
    assert [call.args for call in bot.delete_message.await_args_list] == [(2,11),(2,12)]
    state.clear.assert_awaited_once()
