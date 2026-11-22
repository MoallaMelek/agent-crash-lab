"""Persistence patterns: when (and what) an application writes so a crashed run can resume.

* `final_only`: save `result.all_messages()` once, after the run ends. Nothing survives a crash
  of a running process.
* `iter_naive`: drive the run with `agent.iter` and save `run.all_messages()` after every
  `run.next()`. **Window:** when `run.next()` yields the `ModelRequestNode` that follows a
  `CallToolsNode`, the tool returns are only in `node.request`, not yet in `all_messages()`
  (exp3), so a crash before that next model request finishes loses them.
* `iter_folded`: as `iter_naive`, but when the yielded node is a `ModelRequestNode` its
  `node.request` is appended to what is saved (the same fold StepPersistence does internally,
  harness #373).
* `step`: no application writes; the harness `StepPersistence` capability with a `SqliteStepStore`
  and default options (settled snapshots after each `CallToolsNode`, ledger in
  `before_tool_execute` / `after_tool_execute`).
* `step_frontier`: as `step` with `capture_frontier=True` (also snapshots before each model
  request and at the response frontier; those can be `interrupted`).

History files are written atomically: write a temp file, fsync it, `os.replace`, fsync the
directory. A SIGKILL therefore leaves either the previous or the new complete file.
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, ModelMessagesTypeAdapter, capture_run_messages
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolReturnPart
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import DeferredToolResults
from pydantic_graph import End

from crashlab.config import CellConfig
from crashlab.faults import InjectedCrash, Timeline
from crashlab.scenarios import LabDeps


class HistoryFile:
    """An application-owned history file (JSON via `ModelMessagesTypeAdapter`)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, messages: list[ModelMessage]) -> None:
        tmp = self.path.with_suffix('.tmp')
        data = ModelMessagesTypeAdapter.dump_json(messages)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        dir_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def load(self) -> list[ModelMessage] | None:
        if not self.path.exists():
            return None
        return ModelMessagesTypeAdapter.validate_json(self.path.read_bytes())


def message_shape(m: ModelMessage) -> str:
    """Compact, id-free description of a message, e.g. `response(complete)[tool-call:charge_order]`."""
    kinds: list[str] = []
    for p in m.parts:
        k: str = p.part_kind
        if isinstance(p, ToolReturnPart):
            synth = (p.metadata or {}).get('pydantic_ai_synthesized_tool_return')
            k += f':{p.tool_name}:{p.outcome}{"+synthesized" if synth else ""}'
        elif hasattr(p, 'tool_name'):
            k += f':{p.tool_name}'
        kinds.append(k)
    kind = 'request' if isinstance(m, ModelRequest) else 'response'
    return f'{kind}({m.state})[{", ".join(kinds)}]'


def shapes(messages: list[ModelMessage]) -> list[str]:
    return [message_shape(m) for m in messages]


async def drive(
    agent: Agent[LabDeps, str],
    cell: CellConfig,
    *,
    deps: LabDeps,
    timeline: Timeline,
    history_file: HistoryFile,
    user_prompt: str | None,
    message_history: list[ModelMessage] | None = None,
    deferred_tool_results: DeferredToolResults | None = None,
) -> AgentRunResult[str]:
    """Run the agent to completion, persisting according to `cell.persistence`.

    With `crash='exception'` the run is wrapped in `capture_run_messages()`; when an
    `InjectedCrash` escapes, application-history patterns save the captured messages (the
    pattern `docs/agent.md` recommends for resuming after an error) before re-raising.
    StepPersistence saves its own at-failure snapshot in `on_run_error`.
    """
    pattern = cell.persistence
    capture = capture_run_messages() if cell.crash == 'exception' else nullcontext(None)
    with capture as captured:
        try:
            async with agent.iter(
                user_prompt,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
                deps=deps,
            ) as run:
                node: Any = run.next_node
                while not isinstance(node, End):
                    node = await run.next(node)
                    if pattern in ('iter_naive', 'iter_folded'):
                        msgs = run.all_messages()
                        folded = False
                        if pattern == 'iter_folded' and Agent.is_model_request_node(node):
                            msgs = [*msgs, node.request]
                            folded = True
                        history_file.save(msgs)
                        timeline.append(
                            'history_saved',
                            pattern=pattern,
                            yielded_node=type(node).__name__,
                            folded_request=folded,
                            n_messages=len(msgs),
                            tail=shapes(msgs[-2:]),
                        )
                result = run.result
                assert result is not None
        except InjectedCrash:
            if pattern in ('final_only', 'iter_naive', 'iter_folded') and captured is not None:
                msgs = list(captured)
                history_file.save(msgs)
                timeline.append(
                    'history_saved_on_error',
                    pattern=pattern,
                    source='capture_run_messages',
                    n_messages=len(msgs),
                    tail=shapes(msgs[-2:]),
                )
            raise
    if pattern == 'final_only':
        msgs = result.all_messages()
        history_file.save(msgs)
        timeline.append(
            'history_saved', pattern=pattern, yielded_node='End', n_messages=len(msgs), tail=shapes(msgs[-2:])
        )
    return result


def last_response(messages: list[ModelMessage]) -> ModelResponse | None:
    return next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
