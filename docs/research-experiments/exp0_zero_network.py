"""Experiment 0: deterministic agents with tool calls and zero network."""
import os
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
import pydantic_ai, pydantic_ai.models
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.models.test import TestModel
pydantic_ai.models.ALLOW_MODEL_REQUESTS = False   # any real provider request would raise

agent = Agent(TestModel(call_tools=['charge']))
@agent.tool_plain
def charge(amount_cents: int) -> str:
    return f'charged {amount_cents}'

with capture_run_messages() as msgs:
    r = agent.run_sync('pay')
print(pydantic_ai.__version__, repr(r.output))
print([(type(m).__name__, [p.part_kind for p in m.parts]) for m in r.all_messages()])

try:
    Agent('openai:gpt-4o').run_sync('hi')
except Exception as e:
    print('real model with ALLOW_MODEL_REQUESTS=False ->', type(e).__name__, str(e)[:120])
