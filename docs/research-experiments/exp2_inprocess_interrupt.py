"""Experiment 2: in-process interruption mid tool call (after the side effect, before the tool returns).

For each interruption kind we capture the messages with capture_run_messages, JSON round-trip them,
then resume with no prompt and count side effects.

Kinds:
 - exception raised by the tool after the side effect (RuntimeError)
 - asyncio task cancellation (task.cancel()) while the tool awaits after the side effect
 - KeyboardInterrupt raised inside the tool after the side effect
 - CancellationToken.cancel() while the tool awaits (first-party cancellation -> RunCancelled)
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
sys.path.insert(0, os.path.dirname(__file__))
os.environ['LEDGER'] = tempfile.mktemp(suffix='.ledger')

import pydantic_ai
from pydantic_ai import Agent, CancellationToken, ModelMessagesTypeAdapter, RunContext, capture_run_messages
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import FunctionModel

import exp_common as c

print('pydantic_ai', pydantic_ai.__version__)

MODE = {'kind': None}
gate = asyncio.Event


def build():
    agent = Agent(FunctionModel(c.scripted_model), name='payer')

    @agent.tool
    async def charge(ctx: RunContext[None], amount_cents: int) -> str:
        with c.ledger_path().open('a') as f:
            f.write(f'CHARGE {ctx.tool_call_id}\n')
        kind = MODE['kind']
        if kind == 'exception':
            raise RuntimeError('network died after the charge')
        if kind == 'keyboard':
            raise KeyboardInterrupt
        if kind in ('cancel', 'token'):
            await asyncio.sleep(10)
        return 'charged'

    return agent


def summarize(msgs):
    out = []
    for m in msgs:
        parts = []
        for p in m.parts:
            s = p.part_kind
            if isinstance(p, ToolReturnPart):
                s += f'[{p.outcome}]'
            parts.append(s)
        out.append(f'{type(m).__name__}(state={m.state}):{parts}')
    return out


async def run_case(kind: str):
    MODE['kind'] = kind
    agent = build()
    before = c.charges()
    raised = None
    token = CancellationToken()
    with capture_run_messages() as messages:
        try:
            if kind == 'cancel':
                task = asyncio.create_task(agent.run('pay'))
                await asyncio.sleep(0.2)
                task.cancel()
                await task
            elif kind == 'token':
                task = asyncio.create_task(agent.run('pay', cancellation_token=token))
                await asyncio.sleep(0.2)
                token.cancel()
                await task
            else:
                await agent.run('pay')
        except BaseException as e:  # noqa: BLE001
            raised = f'{type(e).__name__}: {e}'
    captured = list(messages)
    print(f'\n== {kind}: raised {raised}; charges during run={c.charges() - before}')
    for line in summarize(captured):
        print('   captured:', line)
    stored = ModelMessagesTypeAdapter.dump_json(captured)
    MODE['kind'] = None
    before = c.charges()
    res = await agent.run(message_history=ModelMessagesTypeAdapter.validate_json(stored))
    print(f'   resume(no prompt): output={res.output!r} extra charges={c.charges() - before}')


async def main():
    for kind in ('exception', 'cancel', 'token', 'keyboard'):
        await run_case(kind)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    # asyncio re-raises a KeyboardInterrupt raised inside a task out of the event loop:
    # the agent run saw CancelledError, then the whole loop is torn down.
    print('   KeyboardInterrupt escaped asyncio.run() (Python asyncio behaviour) -> process would exit 130')
    print('   ledger charges total:', c.charges())
