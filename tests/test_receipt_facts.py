from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.receipt_facts import Facts, calculate, recognize, translate_for_display


@pytest.fixture(autouse=True)
def display_translation(monkeypatch):
    mock = AsyncMock(return_value={})
    monkeypatch.setattr('app.receipt_facts.translate_for_display', mock)
    return mock


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
                                   response('{"categories":[{"id":2,"category":"Продукты","needs_review":true},{"id":1,"category":"Продукты","needs_review":false}]}')])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    parsed = await recognize(client, 'test-model', [(b'photo', 'image/jpeg')], ['Продукты'])
    assert parsed.amount == Decimal('28.47')
    assert sum((i.amount for i in parsed.items), Decimal(0)) == parsed.amount
    assert create.await_count == 3
    assert [i.category_needs_review for i in parsed.items] == [False, True]
    content = create.call_args.kwargs['messages'][0]['content']
    assert 'source_text' in content[0]['text']
    assert any(part['type'] == 'image_url' for part in content)


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
    monkeypatch.setattr('app.receipt_argon.recognize', recognize_mock)
    parser = AIParser()
    client = object()
    monkeypatch.setattr(parser, '_client_for', lambda runtime: client)
    runtime = FamilyAISettings(openai_api_key=None, openai_model='test')
    assert await parser.parse_receipt_images([], ['Продукты'], runtime) is expected
    recognize_mock.assert_awaited_once_with(client, 'test', [], ['Продукты'], None, None)


@pytest.mark.parametrize('labels', [
    [{'id': 1, 'category': 'Дом', 'needs_review': False}] * 2,
    [{'id': 1, 'category': 'Дом', 'needs_review': False}],
    [{'id': 1, 'category': 'Зарплата', 'needs_review': False},
     {'id': 2, 'category': 'Дом', 'needs_review': False}],
    [{'id': 1, 'category': 'Дом', 'needs_review': False},
     {'id': 999, 'category': 'Дом', 'needs_review': False}],
])
async def test_invalid_classification_mapping_is_rejected(labels):
    import json
    create = AsyncMock(side_effect=[response(json.dumps(sample())),
                                   response(json.dumps({'categories': labels}))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ValueError, match='category mapping'):
        await recognize(client, 'test', [(b'photo', 'image/jpeg')], ['Дом', 'Продукты'])


async def test_categories_follow_ids_not_response_order():
    import json
    labels = [{'id': 2, 'category': 'Дом', 'needs_review': True},
              {'id': 1, 'category': 'Продукты', 'needs_review': False}]
    create = AsyncMock(side_effect=[response(json.dumps(sample())),
                                   response(json.dumps({'categories': labels}))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    parsed = await recognize(client, 'test', [(b'photo', 'image/jpeg')], ['Дом', 'Продукты'])
    assert [(i.category, i.category_needs_review) for i in parsed.items] == [('Продукты', False), ('Дом', True)]


def test_uncertain_category_has_review_link_even_when_collapsed():
    from app.ai import ReceiptItem
    from app.rich_bot import draft_html
    item = ReceiptItem(name='Неясный товар', amount='10', category='Дом', category_needs_review=True)
    for items in ([item], [item.model_dump(mode='json')]):
        html = draft_html('test', Decimal('10'), 'expense', 'Дом', None, items)
        assert 'Проверьте категории' in html
        assert 'txi:test:0' in html
        assert 'Неясный товар' in html


async def test_translation_never_enters_classification(display_translation):
    import json
    data = sample()
    data['products'][0]['name'] = 'מגבונים'
    data['products'][0]['text'] = 'מגבונים 9.50'
    display_translation.return_value = {1: 'Ошибочный перевод: орехи'}
    labels = [{'id': 1, 'category': 'Дом', 'needs_review': False},
              {'id': 2, 'category': 'Продукты', 'needs_review': False}]
    create = AsyncMock(side_effect=[response(json.dumps(data)), response(json.dumps({'categories': labels}))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    parsed = await recognize(client, 'test', [(b'photo', 'image/jpeg')], ['Дом', 'Продукты'])
    item = parsed.items[0]
    assert item.name == 'מגבונים'
    assert item.source_text == 'מגבונים 9.50'
    assert item.category == 'Дом'
    assert item.display_name is None
    display_translation.assert_not_awaited()
    prompt = create.call_args.kwargs['messages'][0]['content'][0]['text']
    assert 'מגבונים' in prompt
    assert 'Ошибочный перевод' not in prompt
    from app.rich_bot import receipt_category_groups, receipt_item_display_name
    assert receipt_category_groups(parsed.items)[0]['names'] == ['מגבונים']
    assert receipt_item_display_name(item) == 'מגבונים'


@pytest.mark.parametrize('payload', ['{}', '{"names":[{"id":1,"text":"Орехи"},{"id":1,"text":"Орехи"}]}'])
async def test_invalid_display_translation_falls_back_to_original(payload):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response(payload)))))
    assert await translate_for_display(client, 'test', Facts.model_validate(sample()).products) == {}


async def test_display_translation_maps_by_id():
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
        return_value=response('{"names":[{"id":2,"text":null},{"id":1,"text":"Масло"}]}')
    ))))
    assert await translate_for_display(client, 'test', Facts.model_validate(sample()).products) == {1: 'Масло', 2: None}


async def test_display_translation_network_failure_is_nonfatal():
    import httpx
    from openai import APIConnectionError
    error = APIConnectionError(request=httpx.Request('POST', 'https://api.openai.com/v1/chat/completions'))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=error))))
    assert await translate_for_display(client, 'test', Facts.model_validate(sample()).products) == {}


def test_numeric_schema_disallows_units_and_currency_symbols():
    import re
    schema = Facts.model_json_schema()
    pattern = schema['properties']['total']['pattern']
    assert re.fullmatch(pattern, '591.32')
    assert not re.fullmatch(pattern, '591.32 ₪')
    assert not re.fullmatch(pattern, '1.012 ק"ג')
