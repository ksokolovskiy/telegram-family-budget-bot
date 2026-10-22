from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.receipt_facts import Facts, calculate, recognize


def sample():
    return {
        'total': '28.47', 'total_evidence': {'page': 1, 'line': 5, 'text': '28.47'},
        'products': [
            {'id': 1, 'page': 1, 'line': 1, 'text': '9.50', 'name': 'butter',
             'quantity': None, 'unit_price': None, 'gross': '9.50'},
            {'id': 2, 'page': 1, 'line': 2, 'text': '2 x 9.50 = 19.00', 'name': 'butter',
             'quantity': '2', 'unit_price': '9.50', 'gross': '19.00'},
        ],
        'deductions': [{'page': 1, 'line': 3, 'text': '-0.03', 'amount': '0.03', 'product_ids': [2]}],
    }


def test_quantity_line_and_tare_are_calculated_once():
    rows = calculate(Facts.model_validate(sample()), 1)
    assert [net for _, net, _ in rows] == [Decimal('9.50'), Decimal('18.97')]


def test_duplicate_source_or_wrong_quantity_is_rejected():
    data = sample()
    data['products'][1]['gross'] = '9.50'
    with pytest.raises(ValueError, match='Quantity'):
        calculate(Facts.model_validate(data), 1)
    data = sample()
    data['deductions'] *= 2
    with pytest.raises(ValueError, match='reused'):
        calculate(Facts.model_validate(data), 1)


def test_receipt_discount_allocates_without_losing_agorot():
    data = sample()
    data['deductions'][0]['product_ids'] = []
    rows = calculate(Facts.model_validate(data), 1)
    assert sum((net for _, net, _ in rows), Decimal(0)) == Decimal('28.47')


def response(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


async def test_independent_verifier_precedes_classification():
    data = sample()
    data['products'][1]['gross'] = '9.50'
    import json
    create = AsyncMock(side_effect=[response(json.dumps(data)), response(json.dumps(sample())),
                                   response('{"categories":["Продукты","Продукты"]}')])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    parsed = await recognize(client, 'test-model', [(b'photo', 'image/jpeg')], ['Продукты'])
    assert parsed.amount == Decimal('28.47')
    assert sum((i.amount for i in parsed.items), Decimal(0)) == parsed.amount
    assert create.await_count == 3


async def test_failed_verification_never_classifies_or_balances():
    import json
    data = sample()
    data['total'] = '100'
    create = AsyncMock(return_value=response(json.dumps(data)))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ValueError, match='still do not reconcile'):
        await recognize(client, 'test-model', [(b'photo', 'image/jpeg')], ['Продукты'])
    assert create.await_count == 2


async def test_verifier_cannot_change_printed_total_to_balance():
    import json
    bad = sample()
    bad['total'] = '100'
    create = AsyncMock(side_effect=[response(json.dumps(bad)), response(json.dumps(sample()))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ValueError, match='still do not reconcile'):
        await recognize(client, 'test-model', [(b'photo', 'image/jpeg')], ['Продукты'])
    assert create.await_count == 2


def test_unknown_discount_target_and_invalid_page_are_rejected():
    data = sample()
    data['deductions'][0]['product_ids'] = [999]
    with pytest.raises(ValueError, match='targets'):
        calculate(Facts.model_validate(data), 1)
    data = sample()
    data['products'][1]['page'] = 2
    with pytest.raises(ValueError, match='source'):
        calculate(Facts.model_validate(data), 1)


def test_fractions_of_agora_are_not_silently_rounded():
    data = sample()
    data['deductions'][0]['amount'] = '0.031'
    with pytest.raises(ValueError, match='precision'):
        calculate(Facts.model_validate(data), 1)


async def test_public_parser_uses_fact_pipeline(monkeypatch):
    from app.ai import AIParser, ParsedTransaction
    from app.settings_store import FamilyAISettings
    expected = ParsedTransaction(amount='1', type='expense', category='Продукты')
    recognize_mock = AsyncMock(return_value=expected)
    monkeypatch.setattr('app.receipt_facts.recognize', recognize_mock)
    parser = AIParser()
    client = object()
    monkeypatch.setattr(parser, '_client_for', lambda runtime: client)
    runtime = FamilyAISettings(openai_api_key=None, openai_model='test')
    assert await parser.parse_receipt_images([], ['Продукты'], runtime) is expected
    recognize_mock.assert_awaited_once_with(client, 'test', [], ['Продукты'], None)
