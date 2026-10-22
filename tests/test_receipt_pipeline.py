from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.receipt_facts import Facts
from app.receipt_pipeline import (
    Assembly, Interpretation, Line, Question, Review, Scan, assemble_rows, reread,
    recognize, validate_evidence,
)


def scan():
    return Scan(lines=[Line(line=1,text='מוצר 10.00',uncertain=False),
                       Line(line=2,text='סהכ 10.00',uncertain=False)])


def facts():
    return Facts.model_validate({'total':'10','total_evidence':{'page':1,'line':2,'text':'סהכ 10.00'},
        'products':[{'id':1,'page':1,'line':1,'text':'מוצר 10.00','name':'מוצר',
                     'quantity':None,'unit_price':None,'gross':'10'}], 'deductions':[]})


def test_identical_purchases_on_one_page_cannot_be_deduplicated():
    plan = Assembly.model_validate({'page_order':[1], 'questions':[], 'duplicates':[
        {'keep':{'page':1,'line':1},'drop':{'page':1,'line':2}}]})
    with pytest.raises(ValueError,match='overlap'):
        assemble_rows([scan()],plan)


def test_cross_page_overlap_retains_source_identity():
    plan = Assembly.model_validate({'page_order':[1,2], 'questions':[], 'duplicates':[
        {'keep':{'page':1,'line':2},'drop':{'page':2,'line':1}}]})
    rows=assemble_rows([scan(),scan()],plan)
    assert [(r['page'],r['line']) for r in rows] == [(1,1),(1,2),(2,2)]


def test_invented_or_rewritten_evidence_is_rejected():
    rows = assemble_rows([scan()],Assembly(page_order=[1],duplicates=[],questions=[]))
    validate_evidence(facts(),rows,1)
    changed=facts()
    changed.products[0].text='different'
    with pytest.raises(ValueError,match='evidence'):
        validate_evidence(changed,rows,1)


async def test_reread_only_replaces_requested_original_rows(monkeypatch):
    response=Scan(lines=[Line(line=1,text='מגבונים 10.00',uncertain=False)])
    ask=AsyncMock(return_value=response)
    monkeypatch.setattr('app.receipt_pipeline.ask',ask)
    pages=[scan()]
    await reread(None,'test',pages,[(b'photo','image/jpeg')],[Question(page=1,line=1,question='Read original name')])
    assert pages[0].lines[0].text=='מגבונים 10.00'
    assert pages[0].lines[1].text==scan().lines[1].text
    assert 'target total' not in ask.call_args.args[4]
    assert '"target_line_ids": [1]' in ask.call_args.args[4]


async def test_reread_rejects_extra_context_rows(monkeypatch):
    monkeypatch.setattr('app.receipt_pipeline.ask', AsyncMock(return_value=scan()))
    pages = [scan()]
    with pytest.raises(ValueError, match='row identity'):
        await reread(None, 'test', pages, [(b'photo', 'image/jpeg')],
                     [Question(page=1, line=1, question='Read line 1 next to line 2')])
    assert pages == [scan()]


async def test_no_classification_before_arithmetic_and_evidence_checks(monkeypatch):
    plan=Assembly(page_order=[1],duplicates=[],questions=[])
    ask=AsyncMock(side_effect=[scan(),plan,Interpretation(facts=facts(),questions=[])])
    classify=AsyncMock(return_value='verified')
    monkeypatch.setattr('app.receipt_pipeline.ask',ask)
    monkeypatch.setattr('app.receipt_pipeline.classify_facts',classify)
    assert await recognize(None,'test',[(b'photo','image/jpeg')],['Дом']) == 'verified'
    assert [call.args[2] for call in ask.call_args_list] == ['receipt_scan','receipt_assembly','receipt_interpretation']
    classify.assert_awaited_once()


async def test_reread_budget_is_bounded(monkeypatch):
    question=Question(page=1,line=1,question='Read name')
    plan=Assembly(page_order=[1],duplicates=[],questions=[question])
    parsed = Interpretation(facts=facts(), questions=[])
    ask=AsyncMock(side_effect=[scan(),plan,parsed,plan,parsed,plan,parsed])
    reread_mock=AsyncMock()
    classify=AsyncMock()
    monkeypatch.setattr('app.receipt_pipeline.ask',ask)
    monkeypatch.setattr('app.receipt_pipeline.reread',reread_mock)
    monkeypatch.setattr('app.receipt_pipeline.classify_facts',classify)
    with pytest.raises(ValueError,match='could not be verified'):
        await recognize(None,'test',[(b'photo','image/jpeg')],['Дом'])
    assert reread_mock.await_count==2
    classify.assert_not_awaited()


async def test_arithmetic_failure_takes_priority_over_layout_questions(monkeypatch):
    layout = Question(page=1, line=1, question='Is the code on its own line?')
    money = Question(page=1, line=1, question='Read the printed monetary amount')
    plan = Assembly(page_order=[1], duplicates=[], questions=[layout])
    bad = facts()
    bad.products[0].gross = Decimal('9')
    ask = AsyncMock(side_effect=[scan(), plan, Interpretation(facts=bad, questions=[layout]),
                                Review(questions=[money]),
                                Assembly(page_order=[1], duplicates=[], questions=[]),
                                Interpretation(facts=facts(), questions=[])])
    reader = AsyncMock()
    monkeypatch.setattr('app.receipt_pipeline.ask', ask)
    monkeypatch.setattr('app.receipt_pipeline.reread', reader)
    monkeypatch.setattr('app.receipt_pipeline.classify_facts', AsyncMock(return_value='verified'))
    assert await recognize(None, 'test', [(b'photo', 'image/jpeg')], ['Дом']) == 'verified'
    assert reader.call_args.args[-1] == [money]
    assert ask.call_args_list[3].args[2] == 'receipt_review'
