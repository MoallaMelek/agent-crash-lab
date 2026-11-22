"""Integration tests: real child processes, real SIGKILL, real FakePay and StepPersistence files.

These are the claims that depend on the process boundary, so they spawn processes on purpose.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from crashlab.config import CellConfig
from crashlab.matrix import QUICK_IDS, curated_by_id
from crashlab.runner import run_cell

pytestmark = pytest.mark.subprocess

CELLS = curated_by_id()

# QUICK_IDS plus cells that exercise the remaining kill points and persistence patterns.
REPRESENTATIVE = (
    *QUICK_IDS,
    'F1-step-after-tool-node',
    'F1-step-after-snapshot-write',
    'F1-notify-iter-naive-next-request',
    'F2-step-frontier-default-origin-stable',
    'F3-iter-naive-reconcile-runid',
    'F4-repair-trust',
    'F4-repair-before-effect-trust',
    'F4-exception-step-interrupted-trust',
    'F5-step-default-reconcile-degraded',
    'F5-notify-history-after-notify',
    'R-iter-naive-restart-after-effect',
)


@pytest.mark.parametrize('cell_id', REPRESENTATIVE)
def test_curated_cell_verdict(cell_id: str) -> None:
    c = CELLS[cell_id]
    rec = run_cell(c.id, c.config)
    assert rec['verdict'] == c.expected, rec['explanation']
    assert rec['lab_error'] is None


def test_configured_kill_is_a_real_sigkill() -> None:
    c = CELLS['H2-naive-history-duplicates']
    rec = run_cell(c.id, c.config)
    a1, a2 = rec['attempts']
    assert a1['returncode'] == -signal.SIGKILL
    a1_events = [e for e in rec['timeline'] if e['attempt'] == 1]
    assert a1_events[-1]['event'] == 'kill', 'nothing may run after the kill marker'
    assert a1_events[-1]['point'] == 'after_effect'
    assert a2['returncode'] == 0
    # The charge committed before the kill is in FakePay; the resume added another.
    assert [r['attempt'] for r in rec['provider']['charges']] == [1, 2]


def test_injected_exception_is_labelled_and_saves_history() -> None:
    c = CELLS['F4-exception-final-only-trust']
    rec = run_cell(c.id, c.config)
    assert rec['attempts'][0]['returncode'] == 3
    assert rec['kill_event']['crash'] == 'exception'
    saved = [e for e in rec['timeline'] if e['event'] == 'history_saved_on_error']
    assert saved
    assert saved[0]['tail'][-1] == 'request(interrupted)[]'


def test_same_cell_twice_has_identical_fingerprint() -> None:
    c = CELLS['F5-parallel-step-frontier-interrupted']
    a = run_cell(c.id, c.config)
    b = run_cell(c.id, c.config)
    assert (a['verdict'], a['effect_counts'], a['fingerprint']) == (b['verdict'], b['effect_counts'], b['fingerprint'])


def test_no_fault_runs_one_attempt() -> None:
    rec = run_cell('baseline', CellConfig(persistence='step_frontier', resume='continue_default'))
    assert rec['verdict'] == 'OK'
    assert len(rec['attempts']) == 1
    assert rec['attempts'][0]['returncode'] == 0
    assert rec['step_store']['snapshots'][-1]['state'] == 'complete'


def test_kill_not_reached_is_reported_not_passed() -> None:
    rec = run_cell('knr', CellConfig(kill='during_next_model_request', kill_tool='notify_customer'))
    assert rec['verdict'] == 'KILL_NOT_REACHED'
    assert len(rec['attempts']) == 1


def test_child_timeout_is_a_lab_error_not_a_finding() -> None:
    rec = run_cell('slow', CellConfig(), timeout=0.01)
    assert rec['verdict'] == 'LAB_ERROR'
    assert 'timed out' in rec['lab_error']


def test_keep_workdir(tmp_path: Path) -> None:
    rec = run_cell('keep', CellConfig(kill='after_effect'), keep_workdir=True, workroot=tmp_path)
    wd = Path(rec['workdir'])
    assert (wd / 'timeline.jsonl').exists()
    assert (wd / 'fakepay.db').exists()
    assert (wd / 'history.json').exists()
