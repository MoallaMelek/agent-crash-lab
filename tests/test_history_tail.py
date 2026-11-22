"""History-tail analysis and resume planning, in-process (no subprocess)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from crashlab.config import CHARGE_TOOL, NOTIFY_TOOL, PROMPT, CellConfig
from crashlab.persistence import HistoryFile, message_shape, shapes
from crashlab.provider import FakePay
from crashlab.resume import dangling_calls, plan_resume

PROMPT_REQ = ModelRequest(parts=[UserPromptPart('go')])


def call(tool: str = CHARGE_TOOL, cid: str = 'call-charge-1') -> ToolCallPart:
    args = (
        {'order_id': 'order-1001', 'amount_cents': 4200}
        if tool == CHARGE_TOOL
        else {'order_id': 'order-1001', 'message': 'm'}
    )
    return ToolCallPart(tool, args, tool_call_id=cid)


def test_dangling_single_call() -> None:
    resp = ModelResponse(parts=[call()], run_id='r1')
    r, calls = dangling_calls([PROMPT_REQ, resp])
    assert r is resp
    assert [c.tool_call_id for c in calls] == ['call-charge-1']


def test_answered_call_is_not_dangling() -> None:
    h = [
        PROMPT_REQ,
        ModelResponse(parts=[call()]),
        ModelRequest(parts=[ToolReturnPart(CHARGE_TOOL, 'ok', 'call-charge-1')]),
    ]
    assert dangling_calls(h)[1] == []


def test_partially_answered_parallel_batch() -> None:
    h = [
        PROMPT_REQ,
        ModelResponse(parts=[call(), call(NOTIFY_TOOL, 'call-notify-1')]),
        ModelRequest(parts=[ToolReturnPart(CHARGE_TOOL, 'ok', 'call-charge-1')]),
    ]
    assert [c.tool_call_id for c in dangling_calls(h)[1]] == ['call-notify-1']


def test_retried_return_answers_a_call() -> None:
    retried = ToolReturnPart(CHARGE_TOOL, 'bad args', 'call-charge-1', outcome='retried')
    h = [PROMPT_REQ, ModelResponse(parts=[call()]), ModelRequest(parts=[retried])]
    assert dangling_calls(h)[1] == []


@pytest.mark.filterwarnings('ignore:`RetryPromptPart` is deprecated')
def test_legacy_retry_prompt_answers_a_call() -> None:
    legacy = RetryPromptPart('bad', tool_name=CHARGE_TOOL, tool_call_id='call-charge-1')
    h = [PROMPT_REQ, ModelResponse(parts=[call()]), ModelRequest(parts=[legacy])]
    assert dangling_calls(h)[1] == []


def test_no_response_and_text_response() -> None:
    assert dangling_calls([PROMPT_REQ]) == (None, [])
    assert dangling_calls([PROMPT_REQ, ModelResponse(parts=[TextPart('hi')])])[1] == []


def test_history_file_round_trip_and_shapes(tmp_path: Path) -> None:
    hf = HistoryFile(tmp_path / 'h.json')
    assert hf.load() is None
    h = [PROMPT_REQ, ModelResponse(parts=[call()])]
    hf.save(h)
    loaded = hf.load()
    assert loaded is not None
    assert shapes(loaded) == ['request(complete)[user-prompt]', 'response(complete)[tool-call:charge_order]']
    assert not (tmp_path / 'h.tmp').exists(), 'temp file is renamed away'
    synth = ToolReturnPart(
        CHARGE_TOOL, 'x', 'c', outcome='interrupted', metadata={'pydantic_ai_synthesized_tool_return': True}
    )
    assert (
        message_shape(ModelRequest(parts=[synth]))
        == 'request(complete)[tool-return:charge_order:interrupted+synthesized]'
    )


def _plan(tmp_path: Path, cell: CellConfig, history: list | None, provider: FakePay):
    hf = HistoryFile(tmp_path / 'history.json')
    if history is not None:
        hf.save(history)
    return asyncio.run(plan_resume(cell, history_file=hf, store=None, provider=provider))


def test_reconcile_injects_found_and_approves_missing(tmp_path: Path, fakepay: FakePay) -> None:
    fakepay.charge('order-1001', 4200, call_ref='call-charge-1')
    h = [PROMPT_REQ, ModelResponse(parts=[call(), call(NOTIFY_TOOL, 'call-notify-1')], run_id='r1')]
    plan = _plan(
        tmp_path, CellConfig(scenario='parallel_batch', resume='reconcile', kill='mid_parallel_batch'), h, fakepay
    )
    assert plan.deferred is not None
    assert list(plan.deferred.calls) == ['call-charge-1']
    assert 'ch_0001' in plan.deferred.calls['call-charge-1']
    assert plan.deferred.approvals == {'call-notify-1': True}
    assert [lk.action for lk in plan.lookups] == ['inject_result', 'execute']


def test_reconcile_with_nothing_found_passes_no_deferred_results(tmp_path: Path, fakepay: FakePay) -> None:
    h = [PROMPT_REQ, ModelResponse(parts=[call()], run_id='r1')]
    plan = _plan(tmp_path, CellConfig(resume='reconcile', kill='before_effect'), h, fakepay)
    assert plan.deferred is None
    assert plan.user_prompt is None


def test_reconcile_uses_the_key_strategy(tmp_path: Path, fakepay: FakePay) -> None:
    fakepay.charge('order-1001', 4200, 'r1:call-charge-1', call_ref='something-else')
    h = [PROMPT_REQ, ModelResponse(parts=[call()], run_id='r1')]
    plan = _plan(tmp_path, CellConfig(resume='reconcile', kill='after_effect', key='run_id+tool_call_id'), h, fakepay)
    assert plan.lookups[0].lookup == 'idempotency_key'
    assert plan.lookups[0].found_effect_id == 'ch_0001'


def test_missing_history_falls_back_to_restart_and_says_so(tmp_path: Path, fakepay: FakePay) -> None:
    plan = _plan(tmp_path, CellConfig(persistence='final_only', resume='reconcile', kill='after_effect'), None, fakepay)
    assert plan.fallback_restart
    assert plan.degraded
    assert plan.user_prompt == PROMPT
    assert any('no persisted history' in n for n in plan.notes)


def test_repair_closes_dangling_calls(tmp_path: Path, fakepay: FakePay) -> None:
    h = [PROMPT_REQ, ModelResponse(parts=[call()], run_id='r1')]
    plan = _plan(tmp_path, CellConfig(resume='repair', kill='after_effect'), h, fakepay)
    assert plan.user_prompt is not None
    assert plan.history is not None
    assert 'interrupted' in shapes(plan.history)[-1]


def test_restart_ignores_history(tmp_path: Path, fakepay: FakePay) -> None:
    h = [PROMPT_REQ, ModelResponse(parts=[call()], run_id='r1')]
    plan = _plan(tmp_path, CellConfig(resume='restart', kill='after_effect'), h, fakepay)
    assert plan.history is None
    assert not plan.fallback_restart
