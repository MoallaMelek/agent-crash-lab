from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

from crashlab.config import CHARGE_TOOL, NOTIFY_TOOL
from crashlab.keys import KeyInputs, KeyStrategyError, derive_key, originating_response, reconstruct_key

T0 = datetime(2026, 11, 22, 12, 0, 0, 123456, tzinfo=UTC)


def _history(
    call_id: str = 'call-1', run_id: str = 'run-A', ts: datetime = T0
) -> tuple[list, ToolCallPart, ModelResponse]:
    call = ToolCallPart(CHARGE_TOOL, {'order_id': 'o1', 'amount_cents': 5}, tool_call_id=call_id)
    resp = ModelResponse(parts=[call], run_id=run_id, timestamp=ts)
    return [ModelRequest(parts=[UserPromptPart('go')]), resp], call, resp


def _inputs(run_id: str | None = 'run-B', call_id: str = 'call-1', messages: list | None = None) -> KeyInputs:
    msgs, _, _ = _history(call_id)
    return KeyInputs(
        tool_name=CHARGE_TOOL,
        tool_call_id=call_id,
        run_id=run_id,
        args={'order_id': 'o1', 'amount_cents': 5},
        messages=msgs if messages is None else messages,
    )


def test_none_has_no_key() -> None:
    assert derive_key('none', _inputs()) is None


def test_run_id_key_changes_with_run_id() -> None:
    assert derive_key('run_id+tool_call_id', _inputs('run-1')) != derive_key('run_id+tool_call_id', _inputs('run-2'))
    with pytest.raises(KeyStrategyError):
        derive_key('run_id+tool_call_id', _inputs(None))


def test_tool_call_id_key_ignores_run_id() -> None:
    assert derive_key('tool_call_id', _inputs('run-1')) == derive_key('tool_call_id', _inputs('run-2')) == 'call-1'


def test_response_origin_uses_the_persisted_response_not_the_current_run() -> None:
    a = derive_key('response_origin', _inputs('run-1'))
    b = derive_key('response_origin', _inputs('run-2'))
    assert a == b
    assert a is not None
    assert a.startswith('run-A:2026-11-22T12:00:00.123456+00:00:')


def test_response_origin_changes_when_the_model_is_asked_again() -> None:
    msgs_later, _, _ = _history(ts=datetime(2026, 11, 22, 12, 0, 1, tzinfo=UTC))
    assert derive_key('response_origin', _inputs(messages=msgs_later)) != derive_key('response_origin', _inputs())


def test_response_origin_requires_the_call_in_messages() -> None:
    with pytest.raises(KeyStrategyError):
        derive_key('response_origin', _inputs(messages=[]))


def test_business_key_is_per_order_and_effect() -> None:
    assert derive_key('business', _inputs()) == 'order:o1:charge'
    notify = KeyInputs(NOTIFY_TOOL, 'x', None, {'order_id': 'o1', 'message': 'm'}, [])
    assert derive_key('business', notify) == 'order:o1:notify'
    with pytest.raises(KeyStrategyError):
        derive_key('business', KeyInputs(CHARGE_TOOL, 'x', None, {}, []))


@pytest.mark.parametrize('strategy', ['none', 'run_id+tool_call_id', 'tool_call_id', 'response_origin', 'business'])
def test_reconstruct_matches_the_original_execution(strategy) -> None:
    """Reconciliation must recompute exactly the key attempt 1 sent, from the persisted call alone."""
    msgs, call, resp = _history(run_id='run-A')
    original = derive_key(strategy, KeyInputs(CHARGE_TOOL, call.tool_call_id, 'run-A', call.args_as_dict(), msgs))
    assert reconstruct_key(strategy, call=call, response=resp, args=call.args_as_dict()) == original


def test_originating_response_picks_newest_carrier() -> None:
    msgs, _, resp = _history()
    assert originating_response(msgs, 'call-1') is resp
    assert originating_response(msgs, 'other') is None
