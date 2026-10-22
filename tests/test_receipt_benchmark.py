import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.receipt_benchmark import measure, summarize


def test_cost_includes_cached_input_and_output_once():
    records = [{'usage': {'prompt_tokens': 1000, 'completion_tokens': 200,
                         'prompt_tokens_details': {'cached_tokens': 400},
                         'completion_tokens_details': {'reasoning_tokens': 100}}}]
    result = summarize(records, (2.5, .25, 15))
    assert result['usd'] == pytest.approx(.0046)
    assert result['usage_complete']
    assert not summarize(records + [{'usage': None}], (2.5, .25, 15))['usage_complete']


async def test_cancelled_call_keeps_unknown_cost_marker():
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=asyncio.CancelledError))))

    async def recognize(proxy, *args):
        try:
            await proxy.chat.completions.create(response_format={'json_schema': {'name': 'scan'}})
        except asyncio.CancelledError:
            raise TimeoutError from None

    result = await measure(client, SimpleNamespace(recognize=recognize), 'test', [], [], None,
                           (2.5, .25, 15))
    assert not result['usage_complete']
    assert result['calls'] == 1
    assert result['records'][0]['error'] == 'CancelledError'
