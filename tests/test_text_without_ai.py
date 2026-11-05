from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai import AIParser, AIUnavailableError, fallback_parse_text
from app.google_ai import GoogleAIError, google_error_message
from app.settings_store import FamilyAISettings


@pytest.mark.parametrize('text,category,comment', [
    ('100', 'Другое', None),
    ('10 продукты', 'Продукты', None),
    ('400 авто заправка', 'Авто', 'заправка'),
    ('25,50 Продукты молоко', 'Продукты', 'молоко'),
    ('100 помидоры', 'Другое', 'помидоры'),
    ('+100 Зарплата', 'Зарплата', None),
])
async def test_simple_input_never_creates_ai_client(monkeypatch, text, category, comment):
    parser = AIParser()
    def forbidden(runtime):
        pytest.fail('Simple operations must not call AI, even with a configured key')
    monkeypatch.setattr(parser, '_client_for', forbidden)
    result = await parser.parse_text(text, ['Дом', 'Продукты', 'Авто', 'Зарплата'], FamilyAISettings(None, 'gpt-5.4', 'google', 'unused'))
    assert result.category == category
    assert result.comment == comment
    assert result.type == ('income' if text.startswith('+') else 'expense')


@pytest.mark.parametrize('text', ['NaN', 'Infinity', '0', '-10', 'молоко'])
def test_invalid_amount_does_not_crash_local_parser(text):
    assert fallback_parse_text(text, ['Продукты']) is None


async def test_google_billing_error_reaches_text_handler(monkeypatch):
    parser = AIParser()
    message = google_error_message(402, {})
    create = AsyncMock(side_effect=GoogleAIError(message))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(parser, '_client_for', lambda runtime: client)
    with pytest.raises(AIUnavailableError, match='предоплаченные'):
        await parser.parse_text('потратил сто на молоко', ['Продукты'], FamilyAISettings(None, 'gpt-5.4', 'google', 'unused'))


def test_rate_limit_and_balance_are_distinct_and_do_not_echo_secrets():
    assert 'предоплаченные' in google_error_message(402, {})
    message = google_error_message(429, {'error': {'message': 'Rate limit: secret-key'}})
    assert 'квота' in message and 'предоплаченные' not in message and 'secret-key' not in message
