"""Evidence files: `results.json` (schema-versioned, complete) and `summary.md` (a table).

Evidence is written once by `crashlab matrix` and never edited by hand. `results.json` records
the exact versions that produced it (read with `importlib.metadata`), the Python version and
platform, the git commit of the lab if available, and for every cell the full record: timeline,
provider rows, StepPersistence ledger and snapshot states, resume plan, oracle output.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from crashlab.config import (
    AFTER_INTERRUPTED,
    CRASH_MODES,
    ID_MODES,
    KEY_STRATEGIES,
    KILL_POINTS,
    PERSISTENCE,
    RESUMES,
    SCENARIOS,
    VERDICTS,
)
from crashlab.matrix import FINDINGS

SCHEMA = 'crashlab.results/v1'
REQUIRED_TOP = ('schema', 'generated_at', 'environment', 'run', 'findings', 'axes', 'summary', 'cells')
REQUIRED_CELL = (
    'id',
    'finding',
    'claim',
    'config',
    'verdict',
    'expected',
    'expectation',
    'effect_counts',
    'explanation',
    'responsibility',
    'checks',
    'timeline',
    'repeats',
    'reproducible',
)


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def git_commit(cwd: Path | None = None) -> dict[str, Any] | None:
    try:
        sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=cwd, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ['git', 'status', '--porcelain'], cwd=cwd, capture_output=True, text=True, timeout=5, check=True
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {'commit': sha, 'dirty': dirty}


def environment() -> dict[str, Any]:
    return {
        'python': sys.version.split()[0],
        'implementation': platform.python_implementation(),
        'platform': platform.platform(),
        'system': platform.system(),
        'machine': platform.machine(),
        'packages': {
            name: _version(name)
            for name in ('pydantic-ai-slim', 'pydantic-ai-harness', 'pydantic-graph', 'pydantic', 'agent-crash-lab')
        },
        'git': git_commit(Path(__file__).resolve().parent),
    }


def build_results(cells: list[dict[str, Any]], *, run_info: dict[str, Any]) -> dict[str, Any]:
    verdicts = Counter(c['verdict'] for c in cells)
    expectations = Counter(c.get('expectation') or 'unchecked' for c in cells)
    return {
        'schema': SCHEMA,
        'generated_at': datetime.now(UTC).isoformat(timespec='seconds'),
        'environment': environment(),
        'run': run_info,
        'findings': FINDINGS,
        'axes': {
            'scenario': list(SCENARIOS),
            'persistence': list(PERSISTENCE),
            'resume': list(RESUMES),
            'key': list(KEY_STRATEGIES),
            'ids': list(ID_MODES),
            'after_interrupted': list(AFTER_INTERRUPTED),
            'kill': list(KILL_POINTS),
            'crash': list(CRASH_MODES),
            'verdict': list(VERDICTS),
        },
        'summary': {
            'cells': len(cells),
            'verdicts': dict(sorted(verdicts.items())),
            'expectations': dict(sorted(expectations.items())),
            'reproducible': sum(1 for c in cells if c.get('reproducible')),
            'lab_errors': verdicts.get('LAB_ERROR', 0),
        },
        'cells': cells,
    }


def validate_results(data: dict[str, Any]) -> list[str]:
    """Problems with a results document (empty list when valid)."""
    problems = [f'missing top-level key {k!r}' for k in REQUIRED_TOP if k not in data]
    if data.get('schema') != SCHEMA:
        problems.append(f'schema is {data.get("schema")!r}, expected {SCHEMA!r}')
    for i, cell in enumerate(data.get('cells', [])):
        for k in REQUIRED_CELL:
            if k not in cell:
                problems.append(f'cell {i} ({cell.get("id")}): missing {k!r}')
        if cell.get('verdict') not in VERDICTS:
            problems.append(f'cell {cell.get("id")}: unknown verdict {cell.get("verdict")!r}')
    return problems


def summary_markdown(results: dict[str, Any]) -> str:
    env = results['environment']
    pk = env['packages']
    git = env.get('git') or {}
    lines = [
        '# Agent Crash Lab: matrix summary',
        '',
        f'Recorded {results["generated_at"]} with pydantic-ai-slim {pk.get("pydantic-ai-slim")}, '
        f'pydantic-ai-harness {pk.get("pydantic-ai-harness")}, Python {env["python"]} on {env["system"]} '
        f'{env["machine"]}; lab commit {git.get("commit", "unknown")[:12]}{" (dirty)" if git.get("dirty") else ""}.',
        '',
        f'{results["summary"]["cells"]} cells, each run {results["run"].get("repeat", 1)} time(s). '
        f'Verdicts: {", ".join(f"{k} {v}" for k, v in results["summary"]["verdicts"].items())}. '
        f'Expectations: {", ".join(f"{k} {v}" for k, v in results["summary"]["expectations"].items())}. '
        f'Reproducible: {results["summary"]["reproducible"]}/{results["summary"]["cells"]}.',
        '',
        'Verdicts count FakePay effects for one order (expected: exactly one charge, plus one notification',
        'where the scenario sends one) and whether the run completed. "Exactly one" here means: in this',
        'scenario, with a provider that honours idempotency keys or supports lookup. Not exactly-once.',
        '',
    ]
    by_finding: dict[str, list[dict[str, Any]]] = {}
    for c in results['cells']:
        by_finding.setdefault(c['finding'], []).append(c)
    for finding, cells in by_finding.items():
        lines += [f'## {finding}: {results["findings"].get(finding, "")}', '']
        lines += [
            '| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |',
            '|---|---|---|---|---|---|---|---|---|---|',
        ]
        for c in cells:
            cfg = c['config']
            eff = ', '.join(f'{k.split("_")[0]} {v}' for k, v in c['effect_counts'].items())
            mark = '' if c.get('expectation') in (None, 'met') else ' **(differs)**'
            kill = cfg['kill'] + (f' ({cfg["crash"]})' if cfg['crash'] != 'sigkill' else '')
            if cfg.get('kill_tool') != 'charge_order':
                kill += f' @{cfg["kill_tool"]}'
            lines.append(
                f'| `{c["id"]}` | {cfg["persistence"]} | {cfg["resume"]} | {cfg["key"]} | {cfg["ids"]} | {kill} | '
                f'{c.get("expected") or "-"} | {c["verdict"]}{mark} | {eff} | {"yes" if c.get("reproducible") else "NO"} |'
            )
        lines.append('')
    return '\n'.join(lines)


def write_evidence(results: dict[str, Any], out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rj = out / 'results.json'
    rj.write_text(json.dumps(results, indent=1, sort_keys=False, default=str) + '\n')
    sm = out / 'summary.md'
    sm.write_text(summary_markdown(results) + '\n')
    return rj, sm


def load_results(path: str | Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(path).read_text())
    problems = validate_results(data)
    if problems:
        raise ValueError('invalid results file:\n  ' + '\n  '.join(problems[:20]))
    return data
