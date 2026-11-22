from __future__ import annotations

import json
from pathlib import Path

import pytest

from crashlab.evidence import SCHEMA, build_results, load_results, summary_markdown, validate_results, write_evidence
from crashlab.faults import Timeline, read_timeline
from crashlab.matrix import CURATED, run_matrix
from crashlab.viewer import banner_text, build_site, render


@pytest.fixture(scope='module')
def results() -> dict:
    # Two N/A cells: exercises the whole evidence pipeline without spawning processes.
    cells = [c for c in CURATED if c.expected == 'NOT_APPLICABLE'][:2]
    records = run_matrix(cells, jobs=1, repeat=2)
    records[0]['claim'] = 'tricky </script><b>claim</b> & more'
    return build_results(records, run_info={'mode': 'test', 'repeat': 2, 'jobs': 1, 'duration_s': 0})


def test_results_schema(results: dict) -> None:
    assert results['schema'] == SCHEMA
    assert validate_results(results) == []
    assert results['environment']['packages']['pydantic-ai-slim'] == '2.71.0'
    assert results['environment']['packages']['pydantic-ai-harness'] == '0.71.0'
    assert results['summary']['verdicts'] == {'NOT_APPLICABLE': 2}
    assert all(c['reproducible'] for c in results['cells'])
    assert all(c['expectation'] == 'met' for c in results['cells'])


def test_validation_reports_problems(results: dict) -> None:
    broken = json.loads(json.dumps(results))
    del broken['cells'][0]['verdict']
    broken['schema'] = 'other'
    problems = validate_results(broken)
    assert any('schema' in p for p in problems)
    assert any("missing 'verdict'" in p for p in problems)


def test_write_and_load(results: dict, tmp_path: Path) -> None:
    rj, sm = write_evidence(results, tmp_path / 'ev')
    assert load_results(rj)['summary'] == results['summary']
    md = sm.read_text()
    assert '| cell | persistence |' in md
    assert 'Not exactly-once' in md
    (tmp_path / 'bad.json').write_text('{"schema": "x"}')
    with pytest.raises(ValueError, match='invalid results'):
        load_results(tmp_path / 'bad.json')
    assert summary_markdown(results).startswith('# Agent Crash Lab')


def test_viewer_is_self_contained_and_inert(results: dict, tmp_path: Path) -> None:
    index = build_site(results, tmp_path / 'site')
    html = index.read_text()
    assert (tmp_path / 'site' / '.nojekyll').exists()
    assert 'This page does not execute anything.' in html
    assert banner_text(results) in html.replace('&#x27;', "'")
    # embedded JSON cannot close its script element or inject markup
    assert '</script><b>' not in html
    assert '\\u003c/script\\u003e' in html
    page = html.split('<script id="crashlab-data"')[0]
    for external in ('http://', 'https://', '<link', '<script src', '@import', 'url('):
        assert external not in page, f'viewer must not load {external!r} resources'
    assert '__CRASHLAB_' not in render(results)


def test_timeline_survives_torn_last_line(tmp_path: Path) -> None:
    t = Timeline(tmp_path / 't.jsonl', attempt=1)
    t.append('a', x=1)
    t.append('b')
    t.close()
    with (tmp_path / 't.jsonl').open('a') as f:
        f.write('{"event": "torn')
    events = read_timeline(tmp_path / 't.jsonl')
    assert [e['event'] for e in events] == ['a', 'b']
    assert events[0]['seq'] == 1
    assert events[1]['seq'] == 2
    assert read_timeline(tmp_path / 'missing.jsonl') == []
