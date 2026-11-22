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


