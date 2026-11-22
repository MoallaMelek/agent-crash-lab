"""Experiment 1: what does Agent.run do with a persisted history whose tail is an
un-answered ToolCallPart?  All in-process, zero network.

Cases:
 A. baseline run (one charge expected)
 B. history = [ModelRequest(prompt), ModelResponse(ToolCallPart)] (JSON round-tripped),
    resume with NO new prompt
 C. same history, resume WITH a new prompt
 D. same history, repair_messages() first, then new prompt
 E. history + trailing ModelRequest(state='interrupted', parts=[]) (what an in-process
    exception/cancel during tool execution leaves), resume with no prompt
 F. ModelResponse(state='interrupted') tail, resume with no prompt
 G. ModelResponse(state='interrupted') tail, resume with a new prompt
 H. `conversation=` path (ConversationTypeAdapter) with dangling call, no prompt
 I. deferred tool results path: resume with DeferredToolResults(calls={'call-1': 'external result'})
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace

sys.path.insert(0, os.path.dirname(__file__))
os.environ['LEDGER'] = tempfile.mktemp(suffix='.ledger')

import pydantic_ai
from pydantic_ai import Conversation, ConversationTypeAdapter, ModelMessagesTypeAdapter
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolReturnPart, repair_messages
from pydantic_ai.tools import DeferredToolResults

import exp_common as c

print('pydantic_ai', pydantic_ai.__version__)
agent = c.make_agent()


def summarize(msgs):
    out = []
    for m in msgs:
        kinds = []
        for p in m.parts:
            k = p.part_kind
            if isinstance(p, ToolReturnPart):
                k += f'[{p.outcome}{",synth" if (p.metadata or {}).get("pydantic_ai_synthesized_tool_return") else ""}]'
            kinds.append(k)
        out.append(f'{type(m).__name__}(state={getattr(m, "state", None)}):{kinds}')
    return out


def case(name, fn):
    before, calls_before = c.charges(), len(c.MODEL_CALLS)
    try:
        res = fn()
        outcome = f'OK output={res.output!r}'
        tail = summarize(res.all_messages())
    except Exception as e:  # noqa: BLE001
        outcome = f'RAISED {type(e).__name__}: {e}'
        tail = []
    print(f'\n== {name}\n   {outcome}\n   new charges={c.charges() - before}  model calls={len(c.MODEL_CALLS) - calls_before}')
    for line in tail:
        print('     ', line)


# A. baseline
result = None


def a():
    global result
    result = agent.run_sync('pay 5 dollars')
    return result


case('A baseline', a)
full = result.all_messages()
# history as it would be persisted right after the model responded, before the tool ran / was recorded
dangling = full[:2]
assert isinstance(dangling[-1], ModelResponse) and dangling[-1].tool_calls
stored = ModelMessagesTypeAdapter.dump_json(dangling)
loaded = lambda: ModelMessagesTypeAdapter.validate_json(stored)  # noqa: E731

case('B dangling ToolCallPart, no prompt', lambda: agent.run_sync(message_history=loaded()))
case('C dangling ToolCallPart, new prompt', lambda: agent.run_sync('are you done?', message_history=loaded()))
case(
    'D repair_messages() then new prompt',
    lambda: agent.run_sync('are you done?', message_history=repair_messages(loaded())),
)
case(
    'E trailing ModelRequest(state=interrupted), no prompt',
    lambda: agent.run_sync(message_history=[*loaded(), ModelRequest(parts=[], state='interrupted')]),
)


def interrupted_resp():
    h = loaded()
    h[-1] = replace(h[-1], state='interrupted')
    return h


case('F ModelResponse(state=interrupted), no prompt', lambda: agent.run_sync(message_history=interrupted_resp()))
case('G ModelResponse(state=interrupted), new prompt', lambda: agent.run_sync('status?', message_history=interrupted_resp()))

conv_json = ConversationTypeAdapter.dump_json(Conversation(messages=loaded(), conversation_id='conv-1'))
case(
    'H conversation= with dangling call, no prompt',
    lambda: agent.run_sync(conversation=ConversationTypeAdapter.validate_json(conv_json)),
)
case(
    'I deferred_tool_results for the dangling call (supply result externally)',
    lambda: agent.run_sync(
        message_history=loaded(), deferred_tool_results=DeferredToolResults(calls={'call-1': 'reconciled: already charged'})
    ),
)
case(
    'J full completed history, no prompt (re-run of a finished run)',
    lambda: agent.run_sync(message_history=ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(full))),
)
print('\nTOTAL charges in ledger:', c.charges())
