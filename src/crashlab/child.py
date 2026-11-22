"""Child process: one attempt of one cell. `python -m crashlab.child <config.json>`.

Exit codes: 0 run completed; 2 the run (or the resume) raised; 3 an injected in-process crash
(`crash='exception'`); -9 (as seen by the parent) a SIGKILL from a kill point. Everything the
parent needs is in the workdir: `timeline.jsonl`, `fakepay.db`, `history.json`, `steps.db`,
`result-a<attempt>.json`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

import pydantic_ai.models
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart

from crashlab.config import CellConfig, ChildConfig
from crashlab.faults import (
    INJECTED_EXCEPTION_EXIT_CODE,
    FaultPlan,
    InjectedCrash,
    InstrumentedSqliteStepStore,
    Timeline,
)
from crashlab.persistence import HistoryFile, drive, shapes
from crashlab.provider import FakePay
from crashlab.resume import plan_resume, step_run_id
from crashlab.scenarios import LabDeps, build_agent

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

HISTORY_FILE = 'history.json'
STEPS_DB = 'steps.db'
FAKEPAY_DB = 'fakepay.db'
TIMELINE = 'timeline.jsonl'


def result_path(workdir: Path, attempt: int) -> Path:
    return workdir / f'result-a{attempt}.json'


def transcript_returns(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, ToolReturnPart):
                    out.append(
                        {
                            'tool': p.tool_name,
                            'tool_call_id': p.tool_call_id,
                            'outcome': p.outcome,
                            'content': str(p.content),
                            'synthesized': bool((p.metadata or {}).get('pydantic_ai_synthesized_tool_return')),
                        }
                    )
    return out


async def run_attempt(cfg: ChildConfig) -> int:
    cell = CellConfig.from_dict(cfg.cell)
    workdir = Path(cfg.workdir)
    attempt = cfg.attempt
    timeline = Timeline(workdir / TIMELINE, attempt=attempt)
    plan = FaultPlan.for_cell(cell, attempt=attempt, timeline=timeline)
    provider = FakePay(workdir / FAKEPAY_DB, attempt=attempt)
    history_file = HistoryFile(workdir / HISTORY_FILE)
    store: InstrumentedSqliteStepStore | None = None
    capability: AbstractCapability[Any] | None = None
    if cell.uses_step_persistence:
        from pydantic_ai_harness.step_persistence import StepPersistence

        store = InstrumentedSqliteStepStore(database=workdir / STEPS_DB, plan=plan)
        capability = StepPersistence(
            store=store, run_id=step_run_id(attempt), capture_frontier=cell.persistence == 'step_frontier'
        )
    agent, model = build_agent(cell, attempt=attempt, plan=plan, persistence_capability=capability)
    deps = LabDeps(cell=cell, provider=provider, plan=plan)
    timeline.append('attempt_started', action=cfg.action, pid=os.getpid(), cell=cell.to_dict())

    out: dict[str, Any] = {'attempt': attempt, 'action': cfg.action, 'completed': False}
    try:
        if cfg.action == 'first_run':
            from crashlab.config import PROMPT

            result = await drive(
                agent, cell, deps=deps, timeline=timeline, history_file=history_file, user_prompt=PROMPT
            )
        else:
            rplan = await plan_resume(cell, history_file=history_file, store=store, provider=provider)
            summary = rplan.summary()
            out['resume'] = summary
            timeline.append('resume_planned', **summary)
            result = await drive(
                agent,
                cell,
                deps=deps,
                timeline=timeline,
                history_file=history_file,
                user_prompt=rplan.user_prompt,
                message_history=rplan.history,
                deferred_tool_results=rplan.deferred,
            )
    except InjectedCrash as e:
        timeline.append('injected_exception_escaped', error=repr(e))
        out['error'] = repr(e)
        result_path(workdir, attempt).write_text(json.dumps(out, indent=1, default=str))
        return INJECTED_EXCEPTION_EXIT_CODE
    except Exception as e:
        timeline.append('run_failed', error_type=type(e).__name__, error=str(e)[:2000])
        out['error'] = f'{type(e).__name__}: {e}'
        out['traceback'] = traceback.format_exc()
        result_path(workdir, attempt).write_text(json.dumps(out, indent=1, default=str))
        return 2
    messages = result.all_messages()
    out.update(
        completed=True,
        output=result.output,
        run_id=result.run_id,
        model_invocations=model.invocations,
        transcript=shapes(messages),
        returns=transcript_returns(messages),
        messages_json=json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
    )
    result_path(workdir, attempt).write_text(json.dumps(out, indent=1, default=str))
    timeline.append('run_completed', output=result.output, run_id=result.run_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print('usage: python -m crashlab.child <config.json>', file=sys.stderr)
        return 64
    cfg = ChildConfig.from_json(Path(args[0]).read_text())
    return asyncio.run(run_attempt(cfg))


if __name__ == '__main__':
    sys.exit(main())
