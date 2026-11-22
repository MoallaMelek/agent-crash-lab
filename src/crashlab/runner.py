"""Parent-side orchestration of one cell: spawn, verify the kill, resume, collect, classify.

For each cell the runner creates a fresh temporary workdir, then:

1. spawns attempt 1 (`python -m crashlab.child <config>`) with a timeout;
2. checks the exit status against the configured fault: `-SIGKILL` and a `kill` line as the last
   attempt-1 timeline event for `crash='sigkill'`, exit 3 for `crash='exception'`, 0 when no fault
   is configured. A configured kill that never fired is `KILL_NOT_REACHED`; any other mismatch is
   a `LAB_ERROR`, never a silent pass;
3. spawns attempt 2 with the resume mechanism (only if attempt 1 crashed);
4. collects FakePay rows, StepPersistence runs / events / snapshots / tool effects, the history
   file, both result files, the timeline and stdout/stderr tails;
5. classifies with `oracle.classify`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from crashlab.child import FAKEPAY_DB, HISTORY_FILE, STEPS_DB, TIMELINE, result_path
from crashlab.config import CHARGE_TOOL, NOTIFY_TOOL, ORDER_ID, CellConfig, ChildConfig, not_applicable_reason
from crashlab.faults import INJECTED_EXCEPTION_EXIT_CODE, read_timeline
from crashlab.oracle import CellFacts, classify

DEFAULT_TIMEOUT_S = 60.0
STREAM_TAIL_CHARS = 4000


class PlatformNotSupported(RuntimeError):
    pass


def ensure_posix() -> None:
    """The lab kills processes with SIGKILL; Windows has no equivalent semantics."""
    if os.name != 'posix' or not hasattr(signal, 'SIGKILL'):
        raise PlatformNotSupported('Agent Crash Lab needs a POSIX system (Linux or macOS): it relies on SIGKILL.')


def _spawn(cfg: ChildConfig, workdir: Path, timeout: float) -> dict[str, Any]:
    cfg_path = workdir / f'child-a{cfg.attempt}.json'
    cfg_path.write_text(cfg.to_json())
    env = {**os.environ, 'PYDANTIC_AI_NO_BANNER': '1', 'PYTHONUNBUFFERED': '1'}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'crashlab.child', str(cfg_path)],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        rc: int | None = proc.returncode
        out, err, timed_out = proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as e:
        rc = None
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or '')
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or '')
        timed_out = True
    return {
        'attempt': cfg.attempt,
        'action': cfg.action,
        'returncode': rc,
        'timed_out': timed_out,
        'duration_s': round(time.monotonic() - started, 3),
        'stdout_tail': out[-STREAM_TAIL_CHARS:],
        'stderr_tail': err[-STREAM_TAIL_CHARS:],
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def collect_provider(workdir: Path) -> dict[str, list[dict[str, Any]]]:
    path = workdir / FAKEPAY_DB
    if not path.exists():
        return {'charges': [], 'notifications': [], 'events': []}
    from crashlab.provider import FakePay

    return FakePay(path).dump()


def collect_step_store(workdir: Path) -> dict[str, Any] | None:
    """Read the StepPersistence database: runs and tool effects with sqlite3 (no list-all API),
    events and snapshots through the public `SqliteStepStore` API."""
    path = workdir / STEPS_DB
    if not path.exists():
        return None
    from pydantic_ai.messages import ModelRequest, ToolReturnPart
    from pydantic_ai_harness.step_persistence import SqliteStepStore

    from crashlab.persistence import shapes

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        runs = [dict(r) for r in conn.execute('SELECT run_id, agent_name, started_at FROM runs ORDER BY started_at')]
        effects = [
            dict(r)
            for r in conn.execute(
                'SELECT run_id, tool_call_id, tool_name, status, started_at, ended_at, idempotency_key, effect_summary '
                'FROM tool_effects ORDER BY started_at'
            )
        ]
    finally:
        conn.close()
    store = SqliteStepStore(database=path)

    async def _read() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        snaps: list[dict[str, Any]] = []
        for run in runs:
            rid = run['run_id']
            for ev in await store.list_events(run_id=rid):
                events.append(
                    {
                        'run_id': rid,
                        'kind': ev.kind,
                        'step_index': ev.step_index,
                        'tool_call_id': ev.tool_call_id,
                        'tool_name': ev.tool_name,
                        'error': ev.error,
                    }
                )
            for s in await store.list_snapshots(run_id=rid, include_interrupted=True):
                snaps.append(
                    {
                        'run_id': rid,
                        'step_index': s.step_index,
                        'state': s.state,
                        'n_messages': len(s.messages),
                        'tail': shapes(s.messages[-2:]),
                        'return_ids': [
                            p.tool_call_id
                            for m in s.messages
                            if isinstance(m, ModelRequest)
                            for p in m.parts
                            if isinstance(p, ToolReturnPart)
                        ],
                    }
                )
        return events, snaps

    events, snaps = asyncio.run(_read())
    return {'runs': runs, 'events': events, 'snapshots': snaps, 'tool_effects': effects}


def collect_history(workdir: Path) -> dict[str, Any] | None:
    path = workdir / HISTORY_FILE
    if not path.exists():
        return None
    from pydantic_ai import ModelMessagesTypeAdapter

    from crashlab.persistence import shapes

    msgs = ModelMessagesTypeAdapter.validate_json(path.read_bytes())
    return {'n_messages': len(msgs), 'shapes': shapes(msgs)}


def run_cell(
    cell_id: str,
    cell: CellConfig,
    *,
    keep_workdir: bool = False,
    workroot: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run one cell end to end and return its full evidence record (JSON-serialisable)."""
    ensure_posix()
    cell.validate()
    started = time.monotonic()
    record: dict[str, Any] = {'id': cell_id, 'config': cell.to_dict(), 'label': cell.label()}
    na = not_applicable_reason(cell)
    if na is not None:
        facts = CellFacts(cell=cell, na_reason=na)
        record.update(_finish(facts, attempts=[], workdir=None, provider=None, step=None, history=None, results={}))
        record['duration_s'] = 0.0
        return record

    workdir = Path(tempfile.mkdtemp(prefix=f'crashlab-{cell_id}-', dir=workroot))
    attempts: list[dict[str, Any]] = []
    facts = CellFacts(cell=cell)
    try:
        a1 = _spawn(
            ChildConfig(cell=cell.to_dict(), attempt=1, workdir=str(workdir), action='first_run'), workdir, timeout
        )
        attempts.append(a1)
        facts.attempt1_rc = a1['returncode']
        timeline = read_timeline(workdir / TIMELINE)
        a1_events = [e for e in timeline if e.get('attempt') == 1]
        kill_events = [e for e in a1_events if e.get('event') == 'kill']
        facts.kill_event = kill_events[0] if kill_events else None
        need_resume = False
        if a1['timed_out']:
            facts.lab_error = f'attempt 1 timed out after {timeout}s'
        elif not cell.expects_crash:
            if a1['returncode'] != 0:
                facts.lab_error = f'attempt 1 (no fault) exited {a1["returncode"]}: {_last_line(a1["stderr_tail"])}'
        elif facts.kill_event is None:
            if a1['returncode'] != 0:
                facts.lab_error = (
                    f'attempt 1 exited {a1["returncode"]} without reaching kill point {cell.kill}: '
                    f'{_last_line(a1["stderr_tail"])}'
                )
            # else: KILL_NOT_REACHED, decided by the oracle
        elif cell.crash == 'sigkill':
            if a1['returncode'] != -signal.SIGKILL:
                facts.lab_error = f'kill marker written but attempt 1 exited {a1["returncode"]}, not -SIGKILL'
            elif a1_events[-1].get('event') != 'kill':
                facts.lab_error = 'attempt 1 wrote timeline events after its kill marker'
            else:
                need_resume = True
        elif a1['returncode'] != INJECTED_EXCEPTION_EXIT_CODE:
            facts.lab_error = f'injected exception, but attempt 1 exited {a1["returncode"]}'
        else:
            need_resume = True

        if need_resume:
            a2 = _spawn(
                ChildConfig(cell=cell.to_dict(), attempt=2, workdir=str(workdir), action='resume'), workdir, timeout
            )
            attempts.append(a2)
            facts.attempt2_rc = a2['returncode']
            if a2['timed_out']:
                facts.final_error = f'attempt 2 timed out after {timeout}s'
            elif a2['returncode'] == -signal.SIGKILL:
                facts.lab_error = 'attempt 2 was SIGKILLed (faults are armed for attempt 1 only)'

        results = {n: _read_json(result_path(workdir, n)) for n in (1, 2)}
        final_attempt = 2 if need_resume else 1
        final = results.get(final_attempt)
        facts.final_attempt = final_attempt
        facts.timeline = read_timeline(workdir / TIMELINE)
        if final is not None:
            facts.completed = bool(final.get('completed'))
            facts.final_output = final.get('output')
            facts.final_returns = final.get('returns', [])
            facts.final_error = facts.final_error or final.get('error')
            if need_resume:
                facts.resume = final.get('resume')
        elif need_resume and facts.final_error is None:
            facts.final_error = (
                f'attempt 2 exited {facts.attempt2_rc} without a result file: {_last_line(attempts[-1]["stderr_tail"])}'
            )
        if need_resume and facts.resume is None:
            planned = [e for e in facts.timeline if e.get('event') == 'resume_planned']
            if planned:
                facts.resume = {k: v for k, v in planned[0].items() if k not in ('attempt', 'seq', 't_ms', 'event')}
        provider = collect_provider(workdir)
        facts.effects = {
            CHARGE_TOOL: [r for r in provider['charges'] if r['order_id'] == ORDER_ID],
            NOTIFY_TOOL: [r for r in provider['notifications'] if r['order_id'] == ORDER_ID],
        }
        step = collect_step_store(workdir) if cell.uses_step_persistence else None
        if step is not None:
            facts.ledger = step['tool_effects']
            facts.snapshots = step['snapshots']
        history = collect_history(workdir)
        record.update(
            _finish(
                facts,
                attempts=attempts,
                workdir=workdir,
                provider=provider,
                step=step,
                history=history,
                results=results,
            )
        )
    except Exception as e:  # the lab itself failed; record it rather than crash the matrix
        facts.lab_error = f'runner error: {type(e).__name__}: {e}'
        record.update(
            _finish(facts, attempts=attempts, workdir=workdir, provider=None, step=None, history=None, results={})
        )
    finally:
        record['duration_s'] = round(time.monotonic() - started, 3)
        if keep_workdir:
            record['workdir'] = str(workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return record


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else '<no stderr>'


def _finish(
    facts: CellFacts,
    *,
    attempts: list[dict[str, Any]],
    workdir: Path | None,
    provider: dict[str, Any] | None,
    step: dict[str, Any] | None,
    history: dict[str, Any] | None,
    results: dict[int, dict[str, Any] | None],
) -> dict[str, Any]:
    c = classify(facts)
    slim_results = {}
    for n, res in results.items():
        if res is None:
            continue
        slim_results[str(n)] = {k: v for k, v in res.items() if k not in ('messages_json', 'traceback')}
        if 'traceback' in res:
            slim_results[str(n)]['traceback_tail'] = str(res['traceback'])[-STREAM_TAIL_CHARS:]
    return {
        'verdict': c.verdict,
        'effect_counts': c.effect_counts,
        'explanation': c.explanation,
        'responsibility': c.responsibility,
        'checks': c.checks,
        'fingerprint': c.fingerprint,
        'not_applicable_reason': facts.na_reason,
        'lab_error': facts.lab_error,
        'completed': facts.completed,
        'final_output': facts.final_output,
        'kill_event': facts.kill_event,
        'attempts': attempts,
        'resume': facts.resume,
        'timeline': facts.timeline,
        'provider': provider,
        'step_store': step,
        'history_file': history,
        'results': slim_results,
    }


def facts_to_dict(facts: CellFacts) -> dict[str, Any]:  # pragma: no cover - debugging helper
    d = asdict(facts)
    d['cell'] = facts.cell.to_dict()
    return d
