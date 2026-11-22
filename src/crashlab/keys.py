"""Effect-key strategies: how a tool derives the idempotency key it sends to the provider.

Every strategy is a pure function of what the tool can see at execution time (`RunContext`,
the validated arguments). The interesting property is *which resume paths keep the key
stable*, because the provider can only dedupe a retry whose key equals the original's.

| strategy | key | stable when | breaks when |
|---|---|---|---|
| `none` | no key | never deduped | any re-execution duplicates |
| `run_id+tool_call_id` | `ctx.run_id:ctx.tool_call_id` | never across runs | every resume: core assigns a new `run_id` and refuses to reuse one already in `message_history` (exp6, #11951) |
| `tool_call_id` | `ctx.tool_call_id` | resume keeps the persisted `ModelResponse` | the resume path asks the model again (restart, StepPersistence complete-only snapshot before the first settled cycle) and the provider mints a new id; also collides across runs if a provider reuses deterministic ids |
| `response_origin` | `ModelResponse.run_id:ModelResponse.timestamp:tool_name:tool_call_id` of the response that carries the call (the #11960 approach) | resume keeps the persisted `ModelResponse` | the model is asked again: a new response has a new timestamp (and usually a new id), even when ids are stable |
| `business` | `order:<order_id>:<effect>` from the arguments | always, for this task | two *intended* identical effects on the same order would be merged; the key encodes a business rule (one charge per order) the application must own |

None of these makes an effect exactly-once by itself: each only helps if the provider honours
the key. The lab's FakePay does (Stripe-like semantics).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from crashlab.config import CHARGE_TOOL, KeyStrategy


class KeyStrategyError(RuntimeError):
    """The strategy could not derive a key from what the tool can see."""


@dataclass(frozen=True)
class KeyInputs:
    """Everything a strategy may read. Built from `RunContext` inside the tool."""

    tool_name: str
    tool_call_id: str
    run_id: str | None
    args: Mapping[str, Any]
    messages: Sequence[ModelMessage]


def effect_name(tool_name: str) -> str:
    return 'charge' if tool_name == CHARGE_TOOL else 'notify'


def originating_response(messages: Sequence[ModelMessage], tool_call_id: str) -> ModelResponse | None:
    """The newest `ModelResponse` in `messages` that carries a `ToolCallPart` with `tool_call_id`."""
    for message in reversed(messages):
        if isinstance(message, ModelResponse) and any(
            isinstance(p, ToolCallPart) and p.tool_call_id == tool_call_id for p in message.parts
        ):
            return message
    return None


def response_origin_key(response: ModelResponse, tool_name: str, tool_call_id: str) -> str:
    if response.run_id is None:
        raise KeyStrategyError('the originating ModelResponse has no run_id')
    return f'{response.run_id}:{response.timestamp.isoformat()}:{tool_name}:{tool_call_id}'


def derive_key(strategy: KeyStrategy, inputs: KeyInputs) -> str | None:
    """The idempotency key for this execution, or None for `strategy='none'`."""
    if strategy == 'none':
        return None
    if strategy == 'run_id+tool_call_id':
        if inputs.run_id is None:
            raise KeyStrategyError('RunContext.run_id is None')
        return f'{inputs.run_id}:{inputs.tool_call_id}'
    if strategy == 'tool_call_id':
        return inputs.tool_call_id
    if strategy == 'response_origin':
        response = originating_response(inputs.messages, inputs.tool_call_id)
        if response is None:
            raise KeyStrategyError(f'no ModelResponse in ctx.messages carries tool_call_id={inputs.tool_call_id!r}')
        return response_origin_key(response, inputs.tool_name, inputs.tool_call_id)
    if strategy == 'business':
        order_id = inputs.args.get('order_id')
        if not isinstance(order_id, str):
            raise KeyStrategyError('business key needs a string `order_id` argument')
        return f'order:{order_id}:{effect_name(inputs.tool_name)}'
    raise KeyStrategyError(f'unknown key strategy {strategy!r}')


def reconstruct_key(
    strategy: KeyStrategy,
    *,
    call: ToolCallPart,
    response: ModelResponse,
    args: Mapping[str, Any],
) -> str | None:
    """Recompute, from a *persisted* dangling call, the key its original execution would have used.

    Used by the `reconcile` resume mechanism. For `run_id+tool_call_id` the original `ctx.run_id`
    is recovered from `ModelResponse.run_id` (core stamps the run id on every message it creates),
    so reconciliation can find a charge whose key the resumed run itself could never reproduce.
    """
    if strategy == 'none':
        return None
    if strategy == 'run_id+tool_call_id':
        if response.run_id is None:
            return None
        return f'{response.run_id}:{call.tool_call_id}'
    if strategy == 'tool_call_id':
        return call.tool_call_id
    if strategy == 'response_origin':
        return response_origin_key(response, call.tool_name, call.tool_call_id)
    return derive_key(
        strategy,
        KeyInputs(tool_name=call.tool_name, tool_call_id=call.tool_call_id, run_id=None, args=args, messages=[]),
    )
