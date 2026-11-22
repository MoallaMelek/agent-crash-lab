"""Crash injection: a fsync'd timeline, kill points, and the probes that fire them.

A kill point is a named boundary in the agent's execution. When the configured point is reached
for the first time, in attempt 1 only, the child process

1. appends a `kill` line to the timeline and fsyncs it, then
2. sends itself `SIGKILL` (`os.kill(os.getpid(), signal.SIGKILL)`), so no `finally`, `atexit`,
   hook or buffered flush runs, or, with `crash='exception'`, raises `InjectedCrash` in-process.

Kill points, relative to the Pydantic AI lifecycle (line references are for pydantic-ai commit
463a6d5, identical to the 2.71.0 / 0.71.0 release code; see docs/ARCHITECTURE.md):

* `before_effect` / `after_effect`: inside the target tool body, before the FakePay call / after
  FakePay committed and before the tool returns.
* `after_tool_node`: in `NodeBoundaryProbe.after_node_run` for the `CallToolsNode` that ran the
  target tool. The probe sits *after* StepPersistence in the capability list, and `after_*`
  hooks dispatch in reverse order (`capabilities/combined.py:664`), so it runs before
  StepPersistence's snapshot save and before `agent.iter`'s caller gets the next node back.
* `during_next_model_request`: inside the scripted model function on the first invocation that
  can see a tool return for the target tool.
* `mid_parallel_batch`: inside `notify_customer`, after `charge_order` (same `ModelResponse`)
  committed its charge and passed every `after_tool_execute` hook (`ToolSettledSignal` is first in
  the list, so its `after_tool_execute` runs last), before the notification's effect.
* `during_snapshot_write` / `after_snapshot_write`: in `InstrumentedSqliteStepStore.save_snapshot`
  for the first snapshot that contains a tool return for the target tool: before delegating to
  `SqliteStepStore.save_snapshot` / after it returned (committed).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import CallToolsNode
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import AgentNode, NodeResult
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolCallPart, ToolReturnPart
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, SqliteStepStore, ToolEffectRecord

from crashlab.config import CellConfig, KillPoint

INJECTED_EXCEPTION_EXIT_CODE = 3
"""Exit code of a child that crashed by `InjectedCrash` and saved what an app would save."""


class InjectedCrash(RuntimeError):
    """Raised instead of SIGKILL when `crash='exception'`. Labelled: an in-process failure."""


class Timeline:
    """Append-only JSONL event log, fsync'd after every line.

    Written before each boundary so the evidence shows exactly where a process died. Survives a
    SIGKILL because each line is in the kernel before the next instruction runs. `t_ms` is wall
    time since the attempt started and is excluded from reproducibility fingerprints.
    """

    def __init__(self, path: str | Path, *, attempt: int) -> None:
        self.path = Path(path)
        self.attempt = attempt
        self._seq = 0
        self._t0 = time.monotonic()
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def append(self, event: str, **fields: Any) -> None:
        self._seq += 1
        record = {
            'attempt': self.attempt,
            'seq': self._seq,
            't_ms': round((time.monotonic() - self._t0) * 1000, 1),
            'event': event,
            **fields,
        }
        line = (json.dumps(record, sort_keys=True, default=str) + '\n').encode()
        os.write(self._fd, line)
        os.fsync(self._fd)

    def close(self) -> None:
        os.close(self._fd)


def read_timeline(path: str | Path) -> list[dict[str, Any]]:
    """Parse a timeline file. A torn last line (impossible for SIGKILL, possible on power loss) is dropped."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@dataclass
class FaultPlan:
    """Which kill point fires, once, in which attempt."""

    kill: KillPoint
    kill_tool: str
    crash: str
    attempt: int
    timeline: Timeline
    armed_attempt: int = 1
    fired: bool = False
    tool_settled: dict[str, asyncio.Event] = field(default_factory=dict)

    @classmethod
    def for_cell(cls, cell: CellConfig, *, attempt: int, timeline: Timeline) -> FaultPlan:
        return cls(kill=cell.kill, kill_tool=cell.kill_tool, crash=cell.crash, attempt=attempt, timeline=timeline)

    def armed(self, point: KillPoint, *, tool: str | None = None) -> bool:
        if self.fired or self.attempt != self.armed_attempt or self.kill != point:
            return False
        return tool is None or tool == self.kill_tool

    def fire(self, point: KillPoint, **detail: Any) -> None:
        """Record the kill in the timeline, then die (or raise). Never returns for SIGKILL."""
        self.fired = True
        self.timeline.append('kill', point=point, crash=self.crash, pid=os.getpid(), **detail)
        if self.crash == 'exception':
            raise InjectedCrash(f'injected crash at {point}')
        os.kill(os.getpid(), signal.SIGKILL)
        time.sleep(30)  # pragma: no cover - SIGKILL cannot be caught; this only runs if it was not delivered
        os._exit(70)  # pragma: no cover

    def maybe_fire(self, point: KillPoint, *, tool: str | None = None, **detail: Any) -> None:
        if self.armed(point, tool=tool):
            self.fire(point, tool=tool, **detail)

    def settled_event(self, tool: str) -> asyncio.Event:
        return self.tool_settled.setdefault(tool, asyncio.Event())


def _calls_in(node: AgentNode[Any]) -> list[ToolCallPart]:
    if isinstance(node, CallToolsNode):
        return [p for p in node.model_response.parts if isinstance(p, ToolCallPart)]
    return []


@dataclass
class ToolSettledSignal(AbstractCapability[Any]):
    """Signals when a tool call passed every `after_tool_execute` hook.

    Must be first in the capability list: `after_tool_execute` dispatches in reverse order
    (`capabilities/combined.py:910`), so this one runs after StepPersistence wrote `completed`.
    """

    plan: FaultPlan | None = None

    async def after_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        assert self.plan is not None
        self.plan.timeline.append('tool_settled', tool=call.tool_name, tool_call_id=call.tool_call_id)
        self.plan.settled_event(call.tool_name).set()
        return result


@dataclass
class NodeBoundaryProbe(AbstractCapability[Any]):
    """Logs every graph node boundary and fires `after_tool_node`.

    Must be last in the capability list, so its `after_node_run` runs before any other
    capability's (including StepPersistence's snapshot save; `capabilities/combined.py:664`).
    """

    plan: FaultPlan | None = None

    async def after_node_run(
        self,
        ctx: RunContext[Any],
        *,
        node: AgentNode[Any],
        result: NodeResult[Any],
    ) -> NodeResult[Any]:
        assert self.plan is not None
        calls = _calls_in(node)
        self.plan.timeline.append(
            'node_finished',
            node=type(node).__name__,
            next=type(result).__name__,
            tool_calls=[[c.tool_name, c.tool_call_id] for c in calls],
        )
        if any(c.tool_name == self.plan.kill_tool for c in calls):
            self.plan.maybe_fire('after_tool_node', tool=self.plan.kill_tool, node='CallToolsNode')
        return result


def snapshot_has_return_for(messages: list[ModelMessage], tool: str) -> bool:
    return any(
        isinstance(p, ToolReturnPart) and p.tool_name == tool
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
    )


class InstrumentedSqliteStepStore(SqliteStepStore):
    """`SqliteStepStore` that logs snapshot and ledger writes and fires the snapshot kill points.

    Behaviour is otherwise unchanged: every call delegates to the stock implementation.
    """

    def __init__(self, *, database: str | Path, plan: FaultPlan) -> None:
        super().__init__(database=database)
        self.plan = plan

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        target = snapshot_has_return_for(snapshot.messages, self.plan.kill_tool)
        if target:
            self.plan.maybe_fire(
                'during_snapshot_write', tool=self.plan.kill_tool, snapshot_state=snapshot.state, committed=False
            )
        await super().save_snapshot(snapshot)
        self.plan.timeline.append(
            'snapshot_saved',
            run_id=snapshot.run_id,
            state=snapshot.state,
            step_index=snapshot.step_index,
            n_messages=len(snapshot.messages),
        )
        if target:
            self.plan.maybe_fire(
                'after_snapshot_write', tool=self.plan.kill_tool, snapshot_state=snapshot.state, committed=True
            )

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        await super().record_tool_effect(record)
        self.plan.timeline.append(
            'ledger_written', run_id=record.run_id, tool_call_id=record.tool_call_id, status=record.status
        )
