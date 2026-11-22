"""Experiment 4: SIGKILL a deterministic Pydantic AI agent at execution boundaries, resume via the
documented mechanisms, and count provider-side charges (expected: exactly 1).

Each cell: fresh workdir -> child phase=first with KILL_AT -> parent SIGKILLs on marker ->
child phase=<resume> -> count charges in provider.jsonl.
"""

from __future__ import annotations

import itertools
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
PY = sys.executable
CHILD = str(HERE / 'exp4_child.py')


def run_child(workdir: Path, persistence: str, tool_mode: str, phase: str, env: dict, kill_marker: str | None):
    proc = subprocess.Popen(
        [PY, CHILD, str(workdir), persistence, tool_mode, phase],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, **env},
    )
    lines = []
    killed = False
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line.rstrip())
        if kill_marker and line.startswith('AT '):
            os.kill(proc.pid, signal.SIGKILL)
            killed = True
            break
    proc.wait(timeout=60)
    err = proc.stderr.read() if proc.stderr else ''
    if proc.returncode not in (0, -9):
        lines.append('STDERR: ' + err.strip().splitlines()[-1] if err.strip() else 'STDERR: <empty>')
    return killed, proc.returncode, lines


def charges(workdir: Path) -> int:
    p = workdir / 'provider.jsonl'
    return len(p.read_text().splitlines()) if p.exists() else 0


rows = []
cells = []
KILLS = ['before_effect', 'after_effect', 'after_tool_node', 'next_model_request']
for persistence, kill_at in itertools.product(['naive_iter', 'folded_iter'], KILLS):
    for tool_mode, phase in [('naive', 'resume_default'), ('idem', 'resume_default'), ('naive', 'resume_reconcile')]:
        cells.append((persistence, kill_at, tool_mode, phase))
for persistence, kill_at in itertools.product(['step', 'step_frontier'], ['before_effect', 'after_effect', 'next_model_request']):
    for tool_mode, phase in [('naive', 'resume_default'), ('naive', 'resume_interrupted'), ('idem', 'resume_interrupted'), ('naive', 'resume_reconcile')]:
        cells.append((persistence, kill_at, tool_mode, phase))

for retry in ['0']:
    for persistence, kill_at, tool_mode, phase in cells:
        wd = Path(tempfile.mkdtemp(prefix='crashlab-'))
        env = {'KILL_AT': kill_at, 'RETRY_ON_INTERRUPTED': retry, 'PYDANTIC_AI_NO_BANNER': '1'}
        killed, rc1, out1 = run_child(wd, persistence, tool_mode, 'first', env, kill_marker='AT ')
        after_kill = charges(wd)
        _, rc2, out2 = run_child(wd, persistence, tool_mode, phase, env, kill_marker=None)
        total = charges(wd)
        verdict = 'OK' if total == 1 else ('DUPLICATE' if total > 1 else 'MISSING')
        info = [l for l in out2 if l.startswith(('HISTORY_TAIL', 'UNRESOLVED', 'RECONCILED', 'OUTPUT', 'RESTARTED', 'LOOKUP', 'STDERR'))]
        rows.append(dict(retry=retry, persistence=persistence, kill_at=kill_at, tool=tool_mode, resume=phase,
                         killed=killed, charges_at_kill=after_kill, charges_final=total, verdict=verdict, resume_log=info))

for r in rows:
    print(f"retry_model={r['retry']} {r['persistence']:11} kill@{r['kill_at']:15} tool={r['tool']:5} {r['resume']:18} "
          f"killed={r['killed']!s:5} charges kill/final={r['charges_at_kill']}/{r['charges_final']} -> {r['verdict']}")
    for l in r['resume_log']:
        print('        ', l[:220])
(HERE / 'exp4_results.json').write_text(json.dumps(rows, indent=1))
