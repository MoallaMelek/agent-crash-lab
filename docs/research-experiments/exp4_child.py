"""Child process for exp4: runs (or resumes) the payment agent and may be SIGKILLed by the parent.

argv: workdir persistence tool_mode phase
  persistence: naive_iter | folded_iter | step
  tool_mode:   naive | idem
  phase:       first | resume_default | resume_interrupted | resume_reconcile
env:
  KILL_AT = before_effect | after_effect | after_tool_node | none  (only used in phase=first)
  RETRY_ON_INTERRUPTED = 1 -> scripted model re-calls `charge` (new id) after an 'interrupted' return
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

import pydantic_ai.models
from pydantic_ai import Agent, ModelMessagesTypeAdapter, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolResults
from pydantic_graph import End

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

workdir, persistence, tool_mode, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
KILL_AT = os.environ.get('KILL_AT', 'none') if phase == 'first' else 'none'
RETRY = os.environ.get('RETRY_ON_INTERRUPTED') == '1'
PROVIDER = workdir / 'provider.jsonl'  # the "payment provider": source of truth for effects
HISTORY = workdir / 'history.json'


def marker(name: str) -> None:
    print(name, flush=True)
    time.sleep(30)  # parent SIGKILLs us while we sleep


def provider_charges() -> list[dict]:
    return [json.loads(line) for line in PROVIDER.read_text().splitlines()] if PROVIDER.exists() else []


def provider_charge(amount: int, key: str | None, ref: str) -> str:
    if key is not None:  # provider-side idempotency: same key -> same charge, no new effect
        for ch in provider_charges():
            if ch['key'] == key:
                return ch['id']
    # `ref` is metadata the provider stores and lets you search by (like Stripe metadata); not a dedupe key
    ch = {'id': f'ch_{len(provider_charges()) + 1}', 'amount': amount, 'key': key, 'ref': ref}
    with PROVIDER.open('a') as f:
        f.write(json.dumps(ch) + '\n')
        f.flush()
        os.fsync(f.fileno())
    return ch['id']


def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = [
        p for m in messages if isinstance(m, ModelRequest) for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == 'charge'
    ]
    if not returns:
        # RANDOM_IDS=1 mimics real providers, which mint a fresh tool_call_id on every response
        cid = f'call-{os.urandom(4).hex()}' if os.environ.get('RANDOM_IDS') == '1' else 'call-1'
        return ModelResponse(parts=[ToolCallPart('charge', {'amount_cents': 500}, tool_call_id=cid)])
    if KILL_AT == 'next_model_request':
        marker('AT next_model_request')  # tool return exists, model is 'thinking'
    last = returns[-1]
    if last.outcome == 'interrupted' and RETRY and len(returns) < 3:
        return ModelResponse(parts=[ToolCallPart('charge', {'amount_cents': 500}, tool_call_id=f'call-{len(returns) + 1}')])
    return ModelResponse(parts=[TextPart(f'done: {last.outcome}: {last.content}')])


capabilities = []
store = None
if persistence.startswith('step'):
    from pydantic_ai_harness import StepPersistence
    from pydantic_ai_harness.step_persistence import FileStepStore

    store = FileStepStore(workdir / 'steps')
    capabilities.append(StepPersistence(store=store, run_id='run-1' if phase == 'first' else None,
                                        capture_frontier=persistence == 'step_frontier'))

agent = Agent(FunctionModel(scripted_model), name='payer', capabilities=capabilities)


@agent.tool
async def charge(ctx: RunContext[None], amount_cents: int) -> str:
    if KILL_AT == 'before_effect':
        marker('AT before_effect')
    key = ctx.tool_call_id if tool_mode == 'idem' else None  # tool_call_id is stable across resume; run_id is not
    charge_id = provider_charge(amount_cents, key, ctx.tool_call_id)
    if KILL_AT == 'after_effect':
        marker('AT after_effect')
    return f'charged {charge_id}'


def save_atomic(messages: list[ModelMessage]) -> None:
    tmp = HISTORY.with_suffix('.tmp')
    tmp.write_bytes(ModelMessagesTypeAdapter.dump_json(messages))
    os.replace(tmp, HISTORY)


async def run_with_iter(**kwargs) -> str:
    async with agent.iter(**kwargs) as run:
        node = run.next_node
        while not isinstance(node, End):
            prev = node
            node = await run.next(node)
            msgs = run.all_messages()
            if persistence == 'folded_iter' and Agent.is_model_request_node(node):
                msgs = [*msgs, node.request]  # tool returns live in the next node's request
            if KILL_AT == 'after_tool_node' and Agent.is_call_tools_node(prev):
                marker('AT after_tool_node')  # tools done, nothing persisted yet
            save_atomic(msgs)
    return run.result.output


def dangling_call_ids(history: list[ModelMessage]) -> list[str]:
    answered = {p.tool_call_id for m in history if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)}
    last = next((m for m in reversed(history) if isinstance(m, ModelResponse)), None)
    return [c.tool_call_id for c in (last.tool_calls if last else []) if c.tool_call_id not in answered]


async def main() -> None:
    if phase == 'first':
        if persistence.startswith('step'):
            out = (await agent.run('pay 5 dollars')).output
        else:
            out = await run_with_iter(user_prompt='pay 5 dollars')
        print('OUTPUT', out)
        return

    # ---- resume phases ----
    if persistence.startswith('step'):
        from pydantic_ai_harness.step_persistence import continue_run

        unresolved = await store.list_unresolved_tool_effects(run_id='run-1')
        print('UNRESOLVED', [(r.tool_call_id, r.status) for r in unresolved])
        try:
            history = await continue_run(store, run_id='run-1', include_interrupted=phase != 'resume_default')
        except LookupError as e:
            print('LOOKUP_ERROR', e)
            history = None
    else:
        history = ModelMessagesTypeAdapter.validate_json(HISTORY.read_bytes()) if HISTORY.exists() else None

    if history is None:
        out = (await agent.run('pay 5 dollars')).output  # nothing to resume: start over
        print('RESTARTED', out)
        return
    print('HISTORY_TAIL', [f'{type(m).__name__}({m.state}):{[p.part_kind for p in m.parts]}' for m in history[-2:]])
    deferred = None
    if phase == 'resume_reconcile':
        # Reconcile dangling calls against the provider (source of truth) before resuming.
        dangling = dangling_call_ids(history)
        done = {ch['ref']: ch['id'] for ch in provider_charges()}
        calls = {cid: f'charged {done[cid]} (reconciled)' for cid in dangling if cid in done}
        if calls:
            deferred = DeferredToolResults(calls=calls)
        print('RECONCILED', calls)
    if persistence.startswith('step'):
        out = (await agent.run(message_history=history, deferred_tool_results=deferred)).output
    else:
        out = await run_with_iter(message_history=history, deferred_tool_results=deferred)
    print('OUTPUT', out)


asyncio.run(main())
