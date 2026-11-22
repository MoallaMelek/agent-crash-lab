from __future__ import annotations

import json
from pathlib import Path

import pytest

from crashlab import cli
from crashlab.config import VERDICTS, not_applicable_reason
from crashlab.matrix import CURATED, FINDINGS, HEADLINE_IDS, QUICK_IDS, curated_by_id, full_product


def test_curated_ids_unique_and_expectations_valid() -> None:
    by_id = curated_by_id()
    assert len(by_id) == len(CURATED)
    for c in CURATED:
        assert c.expected in VERDICTS
        assert c.finding in FINDINGS
        assert c.claim
        assert (not_applicable_reason(c.config) is not None) == (c.expected == 'NOT_APPLICABLE'), c.id


def test_every_finding_is_covered_with_both_outcomes() -> None:
    for f in ('F1', 'F2', 'F3', 'F4', 'F5'):
        verdicts = {c.expected for c in CURATED if c.finding == f}
        assert 'OK' in verdicts, f
        assert 'DUPLICATE' in verdicts, f


def test_headline_and_quick_ids_exist() -> None:
    by_id = curated_by_id()
    assert [by_id[i].expected for i in HEADLINE_IDS] == ['OK', 'DUPLICATE', 'OK', 'OK']
    assert all(i in by_id for i in QUICK_IDS)


def test_full_product_excludes_not_applicable() -> None:
    cells = full_product()
    assert len(cells) > 1000
    assert all(not_applicable_reason(c.config) is None for c in cells)
    assert len({c.id for c in cells}) == len(cells)


def test_cli_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['list', '--finding', 'H']) == 0
    out = capsys.readouterr().out
    assert 'H2-naive-history-duplicates' in out
    assert cli.main(['list', '--json']) == 0
    assert len(json.loads(capsys.readouterr().out)) == len(CURATED)


def test_cli_run_unknown_cell_suggests(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['run', 'H2-naive']) == cli.EXIT_USAGE
    assert 'H2-naive-history-duplicates' in capsys.readouterr().err


def test_cli_bad_override_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['run', 'custom', '--set', 'persistence=nope']) == cli.EXIT_USAGE
    assert cli.main(['run', 'custom', '--set', 'novalue']) == cli.EXIT_USAGE


def test_cli_not_applicable_run_and_matrix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['run', 'D-na-continue-without-store']) == 0
    assert 'NOT_APPLICABLE' in capsys.readouterr().out
    out = tmp_path / 'ev'
    rc = cli.main(
        [
            'matrix',
            '--only',
            'D-na-continue-without-store',
            'D-na-history-with-store',
            '--repeat',
            '2',
            '--out',
            str(out),
            '--check',
            '-q',
        ]
    )
    assert rc == 0
    assert (out / 'results.json').exists()
    assert cli.main(['report', str(out / 'results.json'), '--out', str(tmp_path / 'site')]) == 0
    assert (tmp_path / 'site' / 'index.html').exists()
    assert cli.main(['report', str(tmp_path / 'nope.json'), '--out', str(tmp_path / 's2')]) == cli.EXIT_USAGE
    assert cli.main(['matrix', '--only', 'no-such-cell']) == cli.EXIT_USAGE


def test_cli_doctor_without_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['doctor', '--no-smoke']) == 0
    out = capsys.readouterr().out
    assert 'pydantic-ai-slim     2.71.0' in out
    assert 'POSIX / SIGKILL      ok' in out


def test_format_event() -> None:
    base = {'attempt': 1, 'seq': 3, 't_ms': 1.5}
    kill = cli.format_event(
        {**base, 'event': 'kill', 'crash': 'sigkill', 'point': 'after_effect', 'tool': 'charge_order'}
    )
    assert 'KILL (SIGKILL) at after_effect' in kill
    eff = cli.format_event(
        {
            **base,
            'event': 'effect_committed',
            'tool': 'charge_order',
            'tool_call_id': 'c1',
            'effect_id': 'ch_0001',
            'replayed': True,
        }
    )
    assert 'c1 -> ch_0001 (replayed)' in eff
    plan = cli.format_event(
        {**base, 'event': 'resume_planned', 'mechanism': 'reconcile', 'history_source': 'h', 'fallback_restart': True}
    )
    assert 'reconcile from h FALLBACK restart' in plan
    for ev in (
        'tool_started',
        'model_invoked',
        'model_responded',
        'node_finished',
        'history_saved',
        'snapshot_saved',
        'ledger_written',
        'attempt_started',
        'run_completed',
        'something_else',
    ):
        line = cli.format_event({**base, 'event': ev})
        assert line.startswith('a1 #03')
        assert ev in line
