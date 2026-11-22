"""Experiment 6: small API facts the implementer needs (run_id reuse on resume, ToolDefinition fields,
deferred-tool exceptions, approval flow re-execution)."""
import os, dataclasses
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
import pydantic_ai, pydantic_ai.models
from pydantic_ai import Agent, RunContext, ModelMessagesTypeAdapter, DeferredToolRequests, DeferredToolResults
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, UserError
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
print('pydantic_ai', pydantic_ai.__version__)
print('ToolDefinition fields:', [f.name for f in dataclasses.fields(ToolDefinition)])

def model(messages, info):
    if any(isinstance(p, ToolReturnPart) for m in messages if isinstance(m, ModelRequest) for p in m.parts):
        return ModelResponse(parts=[TextPart('done')])
    return ModelResponse(parts=[ToolCallPart('pay', {'amount': 10}, tool_call_id='c1')])

effects = []
agent = Agent(FunctionModel(model), output_type=[str, DeferredToolRequests])
@agent.tool(requires_approval=True)
async def pay(ctx: RunContext[None], amount: int) -> str:
    effects.append((ctx.run_id, ctx.tool_call_id, ctx.tool_call_approved))
    return 'paid'

r1 = agent.run_sync('pay alice')
print('run1 output type:', type(r1.output).__name__, [c.tool_call_id for c in r1.output.approvals])
stored = ModelMessagesTypeAdapter.dump_json(r1.all_messages())
# approve -> tool runs; simulate "crash before saving" by discarding r2's history, then resume again from stored
for i in range(2):
    r = agent.run_sync(message_history=ModelMessagesTypeAdapter.validate_json(stored),
                       deferred_tool_results=DeferredToolResults(approvals={'c1': True}))
print('approval resumed twice from same stored history -> effects:', len(effects), effects)

# run_id reuse: can a resume pass the original run_id?
orig_run_id = r1.run_id
try:
    agent.run_sync(message_history=ModelMessagesTypeAdapter.validate_json(stored),
                   deferred_tool_results=DeferredToolResults(approvals={'c1': True}), run_id=orig_run_id)
    print('run_id reuse: accepted')
except UserError as e:
    print('run_id reuse -> UserError:', str(e)[:200])
print('message run_ids in stored history:', {m.run_id for m in ModelMessagesTypeAdapter.validate_json(stored)})
