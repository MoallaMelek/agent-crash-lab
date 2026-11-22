"""Shared deterministic agent for the experiments (zero network).

The FunctionModel decides purely from the message history:
- if no ToolReturnPart for `charge` exists yet -> emit one ToolCallPart(charge, id='call-1')
- otherwise -> emit a final text answer that echoes the tool return content/outcome.

The `charge` tool appends a line to a side-effect ledger file (stand-in for a payment API),
so duplicate/missing side effects can be counted across processes.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # guarantees no real provider is ever contacted

MODEL_CALLS: list[int] = []


def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    MODEL_CALLS.append(len(messages))
    returns = [
        p
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.tool_name == 'charge'
    ]
    if not returns:
        return ModelResponse(parts=[ToolCallPart('charge', {'amount_cents': 500}, tool_call_id='call-1')])
    r = returns[-1]
    return ModelResponse(parts=[TextPart(f'done: outcome={r.outcome} content={r.content!r}')])


def ledger_path() -> Path:
    return Path(os.environ.get('LEDGER', '/tmp/crashlab_ledger.txt'))


def make_agent(**kwargs) -> Agent[None, str]:
    agent = Agent(FunctionModel(scripted_model), name='payer', **kwargs)

    @agent.tool
    async def charge(ctx: RunContext[None], amount_cents: int) -> str:
        key = f'{ctx.run_id}:{ctx.tool_call_id}'
        with ledger_path().open('a') as f:
            f.write(f'CHARGE amount={amount_cents} tool_call_id={ctx.tool_call_id} run_id={ctx.run_id}\n')
            f.flush()
            os.fsync(f.fileno())
        hook = os.environ.get('CRASH_AFTER_EFFECT')
        if hook:
            print('EFFECT_DONE', flush=True)
            import time

            time.sleep(60)  # parent SIGKILLs us here
        return f'charged {amount_cents} (key={key})'

    return agent


def charges() -> int:
    p = ledger_path()
    return len(p.read_text().splitlines()) if p.exists() else 0
