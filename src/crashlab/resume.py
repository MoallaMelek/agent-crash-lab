"""Resume mechanisms: what attempt 2 does with whatever attempt 1 left behind.

* `restart`: ignore persisted state; run the same prompt as a fresh run.
* `history`: `agent.run(message_history=<history file>)`, no new prompt. Dangling tool calls in
  the last `ModelResponse` are executed (`_agent_graph.py:1286-1385`).
* `continue_default`: harness `continue_run(store, run_id=...)`, which returns only `complete`
  snapshots, then `agent.run(message_history=...)`.
* `continue_interrupted`: as above with `include_interrupted=True`.
* `repair`: `repair_messages(history)` then a new "continue" prompt. Dangling calls are closed out
  with a synthesized `ToolReturnPart(outcome='interrupted')`; the model decides what to do.
* `reconcile`: find the dangling calls in the newest persisted history (history file, or the
  newest snapshot from `inspect_recovery`, including `interrupted` ones), look each one up in the
  provider, then resume with `DeferredToolResults`: `calls={id: <reconstructed result>}` for
  calls the provider has, and `approvals={id: True}` for calls it does not (so they execute;
  core requires a result for *every* dangling call once any is supplied,
  `_tool_execution.py:557`). This uses `DeferredToolResults` for calls that were never deferred,
  which works on 2.71.0 (exp1 I) but is not documented.

When no persisted history exists (no file; no snapshot `continue_run` will return), every
mechanism except `restart` *falls back to restart* and records why. `reconcile` additionally
records what the ledger and the provider know, because it cannot inject a result for a call no
persisted `ModelResponse` carries.

Guarantees: none beyond what each step states. `reconcile` gives one effect only if the provider
lookup is correct and complete; it is a reference helper, not a framework feature.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    repair_messages,
)
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai_harness.step_persistence import StepStore, continue_run
from pydantic_ai_harness.step_persistence.recovery import inspect_recovery

from crashlab.config import CHARGE_TOOL, CONTINUE_PROMPT, PROMPT, STEP_RUN_ID_TEMPLATE, CellConfig
from crashlab.keys import reconstruct_key
from crashlab.persistence import HistoryFile, shapes
from crashlab.provider import FakePay


@dataclass
class ReconcileLookup:
    tool_call_id: str
    tool_name: str
    lookup: str
    """`idempotency_key` or `call_ref` (metadata search)."""
    key: str | None
    found_effect_id: str | None
    action: str
    """`inject_result` or `execute`."""


@dataclass
class ResumePlan:
    """What attempt 2 will pass to `agent.iter`, and why. Serialised into the evidence."""

    mechanism: str
    history_source: str
    history: list[ModelMessage] | None
    user_prompt: str | None
    deferred: DeferredToolResults | None = None
    fallback_restart: bool = False
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    lookups: list[ReconcileLookup] = field(default_factory=list)
    recovery_facts: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            'mechanism': self.mechanism,
            'history_source': self.history_source,
            'history_tail': shapes(self.history[-3:]) if self.history else None,
            'history_len': len(self.history) if self.history is not None else None,
            'user_prompt': self.user_prompt,
            'deferred_calls': sorted(self.deferred.calls) if self.deferred else [],
            'deferred_approvals': sorted(self.deferred.approvals) if self.deferred else [],
            'fallback_restart': self.fallback_restart,
            'degraded': self.degraded,
            'notes': self.notes,
            'lookups': [asdict(x) for x in self.lookups],
            'recovery_facts': self.recovery_facts,
        }


def dangling_calls(history: list[ModelMessage]) -> tuple[ModelResponse | None, list[ToolCallPart]]:
    """Tool calls in the newest `ModelResponse` that no later request answers."""
    idx = next((i for i in range(len(history) - 1, -1, -1) if isinstance(history[i], ModelResponse)), None)
    if idx is None:
        return None, []
    response = history[idx]
    assert isinstance(response, ModelResponse)
    answered: set[str] = set()
    for m in history[idx + 1 :]:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, ToolReturnPart | RetryPromptPart):
                    answered.add(p.tool_call_id)
    return response, [p for p in response.parts if isinstance(p, ToolCallPart) and p.tool_call_id not in answered]


def step_run_id(attempt: int) -> str:
    return STEP_RUN_ID_TEMPLATE.format(attempt=attempt)


def _restart(mechanism: str, source: str, notes: list[str]) -> ResumePlan:
    return ResumePlan(
        mechanism=mechanism,
        history_source=source,
        history=None,
        user_prompt=PROMPT,
        fallback_restart=mechanism != 'restart',
        notes=notes,
    )


async def _load_step_history(store: StepStore, *, include_interrupted: bool) -> list[ModelMessage] | None:
    try:
        return await continue_run(store, run_id=step_run_id(1), include_interrupted=include_interrupted)
    except LookupError:
        return None


async def recovery_facts(store: StepStore) -> dict[str, Any]:
    """What `inspect_recovery` reports for attempt 1's run (facts only; no replay decision)."""
    insp = await inspect_recovery(store=store, run_id=step_run_id(1))
    return {
        'latest_state': insp.latest.state if insp.latest else None,
        'latest_tail': shapes(insp.latest.messages[-2:]) if insp.latest else None,
        'settled_tail': shapes(insp.settled.messages[-2:]) if insp.settled else None,
        'unresolved': [[r.tool_name, r.tool_call_id, r.status] for r in insp.unresolved],
        'completed_tools': list(insp.completed_tools),
        'failed_tools': list(insp.failed_tools),
    }


def _lookup(provider: FakePay, cell: CellConfig, call: ToolCallPart, response: ModelResponse) -> ReconcileLookup:
    args = call.args_as_dict()
    key = reconstruct_key(cell.key, call=call, response=response, args=args)
    found: str | None
    if call.tool_name == CHARGE_TOOL:
        c = provider.find_by_key(key) if key is not None else provider.find_by_call_ref(call.tool_call_id)
        found = c.charge_id if c else None
    else:
        n = (
            provider.find_notification_by_key(key)
            if key is not None
            else provider.find_notification_by_call_ref(call.tool_call_id)
        )
        found = n.notification_id if n else None
    return ReconcileLookup(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        lookup='idempotency_key' if key is not None else 'call_ref',
        key=key,
        found_effect_id=found,
        action='inject_result' if found else 'execute',
    )


def reconstructed_result(lookup: ReconcileLookup) -> str:
    verb = 'charged' if lookup.tool_name == CHARGE_TOOL else 'notified'
    return f'{verb} {lookup.found_effect_id} (reconciled from provider)'


async def plan_resume(
    cell: CellConfig,
    *,
    history_file: HistoryFile,
    store: StepStore | None,
    provider: FakePay,
) -> ResumePlan:
    """Decide what attempt 2 runs. Never executes tools or writes to the provider."""
    mech = cell.resume
    step = cell.uses_step_persistence
    facts: dict[str, Any] = {}
    if step:
        assert store is not None
        facts = await recovery_facts(store)

    if mech == 'restart':
        plan = _restart(mech, 'none (restart ignores persisted state)', [])
        plan.recovery_facts = facts
        return plan

    # Load the history each mechanism reads.
    if mech in ('continue_default', 'continue_interrupted'):
        assert store is not None
        include = mech == 'continue_interrupted'
        source = f'continue_run(include_interrupted={include})'
        history = await _load_step_history(store, include_interrupted=include)
    elif step:
        assert store is not None
        source = 'continue_run(include_interrupted=True)'
        history = await _load_step_history(store, include_interrupted=True)
    else:
        source = history_file.path.name
        history = history_file.load()

    if history is None:
        why = f'{source}: LookupError (no continuable snapshot)' if step else f'{source}: no persisted history'
        plan = _restart(mech, source, [f'{why}; the application falls back to restarting the task'])
        plan.recovery_facts = facts
        if mech == 'reconcile':
            plan.degraded = True
            unresolved = facts.get('unresolved', [])
            for tool, call_id, status in unresolved:
                if tool == CHARGE_TOOL:
                    hit = provider.find_by_call_ref(call_id)
                    hit_id = hit.charge_id if hit else None
                else:
                    nhit = provider.find_notification_by_call_ref(call_id)
                    hit_id = nhit.notification_id if nhit else None
                plan.notes.append(
                    f'ledger: {tool} {call_id} is {status!r}; provider lookup by call_ref -> {hit_id or "not found"}; '
                    'no persisted ModelResponse carries this call, so no result can be injected'
                )
            if not unresolved:
                plan.notes.append('no ledger available to consult' if not step else 'ledger has no unresolved effects')
        return plan

    if mech in ('history', 'continue_default', 'continue_interrupted'):
        return ResumePlan(
            mechanism=mech, history_source=source, history=history, user_prompt=None, recovery_facts=facts
        )

    if mech == 'repair':
        repaired = repair_messages(history)
        notes = []
        if shapes(repaired) != shapes(history):
            notes.append('repair_messages changed the history (dangling calls closed as interrupted)')
        return ResumePlan(
            mechanism=mech,
            history_source=source,
            history=repaired,
            user_prompt=CONTINUE_PROMPT,
            notes=notes,
            recovery_facts=facts,
        )

    assert mech == 'reconcile'
    response, dangling = dangling_calls(history)
    plan = ResumePlan(mechanism=mech, history_source=source, history=history, user_prompt=None, recovery_facts=facts)
    if response is None or not dangling:
        plan.notes.append('no dangling tool calls in the persisted history; resuming it unchanged')
        return plan
    for call in dangling:
        plan.lookups.append(_lookup(provider, cell, call, response))
    calls = {lk.tool_call_id: reconstructed_result(lk) for lk in plan.lookups if lk.found_effect_id}
    if calls:
        approvals: dict[str, bool] = {lk.tool_call_id: True for lk in plan.lookups if not lk.found_effect_id}
        plan.deferred = DeferredToolResults(calls=dict(calls), approvals=dict(approvals))
    else:
        plan.notes.append('provider has none of the dangling effects; they execute normally')
    return plan
