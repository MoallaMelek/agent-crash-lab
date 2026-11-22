"""Experiment 3: what does `agent.iter` expose at each node boundary, i.e. what can an
application persist and when?  Shows that the tool returns live in the *next* node's
`request` and are not yet in `run.all_messages()` when the ModelRequestNode is yielded.
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
from pydantic_ai import Agent
from pydantic_graph import End

import exp_common as c

print('pydantic_ai', pydantic_ai.__version__)


def shape(msgs):
    return [f'{type(m).__name__}:{[p.part_kind for p in m.parts]}' for m in msgs]


async def main():
    agent = c.make_agent()
    async with agent.iter('pay', run_id='fixed-run') as run:
        print('start  all_messages:', shape(run.all_messages()))
        node = run.next_node
        while not isinstance(node, End):
            print(f'about to run {type(node).__name__:18}; charges so far={c.charges()}')
            node = await run.next(node)
            extra = ''
            if Agent.is_model_request_node(node):
                extra = f'  next.request parts={[p.part_kind for p in node.request.parts]}'
            print(f'   -> yielded {type(node).__name__:18} all_messages={shape(run.all_messages())}{extra}')
    print('result run_id', run.result.run_id, 'output', run.result.output)


asyncio.run(main())
