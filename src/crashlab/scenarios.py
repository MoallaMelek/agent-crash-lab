"""Deterministic agents: a scripted `FunctionModel` plus side-effecting tools backed by FakePay.

No network and no LLM: `pydantic_ai.models.ALLOW_MODEL_REQUESTS` is set to False on import, so
any non-test model would refuse to run. The scripted model decides only from the message
history it is given, so a resumed process makes the same decision a fresh one would.

Scenarios:

* `single_charge`: prompt -> `charge_order(order_id, amount_cents)` -> final text.
* `charge_then_notify`: charge, then (next model turn) `notify_customer`, then final text.
* `parallel_batch`: `charge_order` and `notify_customer` in one `ModelResponse`; Pydantic AI's
  default `'parallel'` execution mode runs them concurrently.

Model options:

* `ids='stable'`: tool-call ids are a pure function of the history (`call-charge-1`,
  `call-charge-2` for a re-issue, ...), so a model asked again in another process emits the
  same id. This is *kinder* than real providers.
* `ids='fresh'`: ids are `call-a<attempt>-n<k>-<j>` (process attempt, model invocation, part
  index): different in every process, reproducible across lab runs, like a real provider that
  mints a new id on every response.
* `after_interrupted='trust'`: on a `ToolReturnPart(outcome='interrupted')` the model stops and
  reports that the status is unknown. `'reissue'`: it issues a *new* call for the same effect
  (what an LLM reasoning "the payment may not have gone through" plausibly does; exp5).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pydantic_ai.models
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from crashlab.config import AMOUNT_CENTS, CHARGE_TOOL, NOTIFY_TOOL, ORDER_ID, CellConfig
from crashlab.faults import FaultPlan
from crashlab.keys import KeyInputs, derive_key
from crashlab.provider import FakePay

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

MAX_CALLS_PER_TOOL = 3
"""A re-issuing model gives up after this many calls for one effect (keeps runs finite)."""

NOTIFY_MESSAGE = f'Your order {ORDER_ID} was charged {AMOUNT_CENTS} cents.'


@dataclass
class LabDeps:
    """Run dependencies handed to tools through `RunContext.deps`."""

    cell: CellConfig
    provider: FakePay
    plan: FaultPlan


def _returns_by_tool(messages: list[ModelMessage]) -> dict[str, list[ToolReturnPart]]:
    out: dict[str, list[ToolReturnPart]] = defaultdict(list)
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, ToolReturnPart):
                    out[p.tool_name].append(p)
    return out


def _calls_by_tool(messages: list[ModelMessage]) -> dict[str, list[ToolCallPart]]:
    out: dict[str, list[ToolCallPart]] = defaultdict(list)
    for m in messages:
        if isinstance(m, ModelResponse):
            for p in m.parts:
                if isinstance(p, ToolCallPart):
                    out[p.tool_name].append(p)
    return out


def _short(tool: str) -> str:
    return 'charge' if tool == CHARGE_TOOL else 'notify'


def _args_for(tool: str) -> dict[str, Any]:
    if tool == CHARGE_TOOL:
        return {'order_id': ORDER_ID, 'amount_cents': AMOUNT_CENTS}
    return {'order_id': ORDER_ID, 'message': NOTIFY_MESSAGE}


@dataclass
class ScriptedModel:
    """The `FunctionModel` function. Deterministic given (history, options, attempt, invocation)."""

    cell: CellConfig
    attempt: int
    plan: FaultPlan
    invocations: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.invocations += 1
        returns = _returns_by_tool(messages)
        calls = _calls_by_tool(messages)
        seen = {tool: [r.outcome for r in rs] for tool, rs in returns.items()}
        self.plan.timeline.append('model_invoked', k=self.invocations, sees_returns=seen)
        if returns.get(self.plan.kill_tool):
            self.plan.maybe_fire('during_next_model_request', tool=self.plan.kill_tool, k=self.invocations)
        response = self._decide(returns, calls)
        emitted = [
            [p.tool_name, p.tool_call_id] if isinstance(p, ToolCallPart) else ['text', p.content]
            for p in response.parts
            if isinstance(p, ToolCallPart | TextPart)
        ]
        self.plan.timeline.append('model_responded', k=self.invocations, emits=emitted)
        return response

    # -- decisions ---------------------------------------------------------

    def _new_calls(self, tools: list[str], calls: dict[str, list[ToolCallPart]]) -> ModelResponse:
        parts: list[ToolCallPart] = []
        for j, tool in enumerate(tools, start=1):
            if self.cell.ids == 'stable':
                call_id = f'call-{_short(tool)}-{len(calls.get(tool, [])) + 1}'
            else:
                call_id = f'call-a{self.attempt}-n{self.invocations}-{j}'
            parts.append(ToolCallPart(tool, _args_for(tool), tool_call_id=call_id))
        return ModelResponse(parts=parts)

    def _status(self, tool: str, returns: dict[str, list[ToolReturnPart]]) -> str:
        rs = returns.get(tool)
        if not rs:
            return 'not_called'
        return 'done' if rs[-1].outcome == 'success' else 'unsettled'

    def _decide(self, returns: dict[str, list[ToolReturnPart]], calls: dict[str, list[ToolCallPart]]) -> ModelResponse:
        scenario = self.cell.scenario
        if scenario == 'parallel_batch':
            sequence = [[CHARGE_TOOL, NOTIFY_TOOL]]
        elif scenario == 'charge_then_notify':
            sequence = [[CHARGE_TOOL], [NOTIFY_TOOL]]
        else:
            sequence = [[CHARGE_TOOL]]
        for group in sequence:
            statuses = {tool: self._status(tool, returns) for tool in group}
            if all(s == 'done' for s in statuses.values()):
                continue
            if all(s == 'not_called' for s in statuses.values()):
                return self._new_calls(group, calls)
            unsettled = [t for t, s in statuses.items() if s == 'unsettled']
            if self.cell.after_interrupted == 'reissue':
                retry = [t for t in unsettled if len(calls.get(t, [])) < MAX_CALLS_PER_TOOL]
                if retry:
                    return self._new_calls(retry, calls)
            outcomes = ', '.join(f'{t}={returns[t][-1].outcome}' for t in unsettled)
            return ModelResponse(
                parts=[TextPart(f'STATUS UNKNOWN: {outcomes}. Not retrying; a human should check the provider.')]
            )
        summary = '; '.join(f'{t}: {returns[t][-1].content}' for group in sequence for t in group)
        return ModelResponse(parts=[TextPart(f'DONE: {summary}')])


def _key_inputs(ctx: RunContext[LabDeps], args: dict[str, Any]) -> KeyInputs:
    assert ctx.tool_name is not None
    assert ctx.tool_call_id is not None
    return KeyInputs(
        tool_name=ctx.tool_name,
        tool_call_id=ctx.tool_call_id,
        run_id=ctx.run_id,
        args=args,
        messages=list(ctx.messages),
    )


async def charge_order(ctx: RunContext[LabDeps], order_id: str, amount_cents: int) -> str:
    """Charge the customer's card for an order (FakePay)."""
    d = ctx.deps
    tl = d.plan.timeline
    tl.append('tool_started', tool=CHARGE_TOOL, tool_call_id=ctx.tool_call_id, run_id=ctx.run_id)
    d.plan.maybe_fire('before_effect', tool=CHARGE_TOOL, tool_call_id=ctx.tool_call_id)
    key = derive_key(d.cell.key, _key_inputs(ctx, {'order_id': order_id, 'amount_cents': amount_cents}))
    res = d.provider.charge(order_id, amount_cents, key, call_ref=ctx.tool_call_id)
    tl.append(
        'effect_committed',
        tool=CHARGE_TOOL,
        tool_call_id=ctx.tool_call_id,
        effect_id=res.charge_id,
        replayed=res.replayed,
        idempotency_key=key,
    )
    d.plan.maybe_fire('after_effect', tool=CHARGE_TOOL, tool_call_id=ctx.tool_call_id, effect_id=res.charge_id)
    return f'charged {res.charge_id}' + (' (replayed by provider)' if res.replayed else '')


async def notify_customer(ctx: RunContext[LabDeps], order_id: str, message: str) -> str:
    """Send the customer a notification about the order (FakePay)."""
    d = ctx.deps
    tl = d.plan.timeline
    tl.append('tool_started', tool=NOTIFY_TOOL, tool_call_id=ctx.tool_call_id, run_id=ctx.run_id)
    if d.plan.armed('mid_parallel_batch'):
        # Wait (bounded) until the charge in the same batch has committed and settled.
        try:
            await asyncio.wait_for(d.plan.settled_event(CHARGE_TOOL).wait(), timeout=10)
        except TimeoutError:
            tl.append('kill_point_wait_timeout', point='mid_parallel_batch')
        else:
            d.plan.fire('mid_parallel_batch', tool=NOTIFY_TOOL, tool_call_id=ctx.tool_call_id)
    d.plan.maybe_fire('before_effect', tool=NOTIFY_TOOL, tool_call_id=ctx.tool_call_id)
    key = derive_key(d.cell.key, _key_inputs(ctx, {'order_id': order_id, 'message': message}))
    res = d.provider.notify(order_id, message, key, call_ref=ctx.tool_call_id)
    tl.append(
        'effect_committed',
        tool=NOTIFY_TOOL,
        tool_call_id=ctx.tool_call_id,
        effect_id=res.notification_id,
        replayed=res.replayed,
        idempotency_key=key,
    )
    d.plan.maybe_fire('after_effect', tool=NOTIFY_TOOL, tool_call_id=ctx.tool_call_id, effect_id=res.notification_id)
    return f'notified {res.notification_id}' + (' (replayed by provider)' if res.replayed else '')


def build_agent(
    cell: CellConfig,
    *,
    attempt: int,
    plan: FaultPlan,
    persistence_capability: AbstractCapability[Any] | None,
) -> tuple[Agent[LabDeps, str], ScriptedModel]:
    """Assemble the agent. Capability order matters and is part of the kill-point definitions."""
    from crashlab.faults import NodeBoundaryProbe, ToolSettledSignal

    model_fn = ScriptedModel(cell=cell, attempt=attempt, plan=plan)
    capabilities: list[AbstractCapability[Any]] = [ToolSettledSignal(plan=plan)]
    if persistence_capability is not None:
        capabilities.append(persistence_capability)
    capabilities.append(NodeBoundaryProbe(plan=plan))
    agent: Agent[LabDeps, str] = Agent(
        FunctionModel(model_fn, model_name='crashlab-scripted'),
        name='crashlab-payer',
        deps_type=LabDeps,
        output_type=str,
        tools=[charge_order] if cell.scenario == 'single_charge' else [charge_order, notify_customer],
        capabilities=capabilities,
    )
    return agent, model_fn
