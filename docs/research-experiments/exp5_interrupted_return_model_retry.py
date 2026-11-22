"""Experiment 5: in-process failure after the side effect -> history gets ModelRequest(state='interrupted').
Resuming closes the call with a synthesized outcome='interrupted' return (no re-execution by the framework),
but the *model* now decides. A model that re-issues the call (plausible LLM behaviour; INFERRED) causes a
duplicate effect that no framework layer would catch."""
import asyncio, os
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
import pydantic_ai, pydantic_ai.models
from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
effects = []
fail = {'on': True}

def model(messages, info):
    rets = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
    if not rets:
        return ModelResponse(parts=[ToolCallPart('charge', {'amount_cents': 500}, tool_call_id='call-1')])
    if rets[-1].outcome == 'interrupted':  # "the payment may not have gone through, let me try again"
        return ModelResponse(parts=[ToolCallPart('charge', {'amount_cents': 500}, tool_call_id='call-2')])
    return ModelResponse(parts=[TextPart(f'done ({rets[-1].content})')])

agent = Agent(FunctionModel(model))

@agent.tool
async def charge(ctx: RunContext[None], amount_cents: int) -> str:
    effects.append(ctx.tool_call_id)
    if fail['on']:
        fail['on'] = False
        raise ConnectionError('connection reset after provider accepted the charge')
    return f'charged for {ctx.tool_call_id}'

async def main():
    with capture_run_messages() as msgs:
        try:
            await agent.run('pay')
        except ConnectionError as e:
            print('first run raised:', repr(e))
    print('tail:', [(type(m).__name__, m.state) for m in msgs[-2:]])
    res = await agent.run(message_history=list(msgs))
    print('resume output:', res.output)
    print('effects (tool_call_ids that executed):', effects, '-> duplicate' if len(effects) > 1 else '')
print('pydantic_ai', pydantic_ai.__version__)
asyncio.run(main())
