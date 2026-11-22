from __future__ import annotations

from typing import Any

from crashlab.config import CellConfig
from crashlab.oracle import CellFacts, classify, fingerprint


def charge(i: int, attempt: int, call: str = 'call-charge-1', key: str | None = None) -> dict[str, Any]:
    return {'id': f'ch_000{i}', 'order_id': 'order-1001', 'idempotency_key': key, 'call_ref': call, 'attempt': attempt}


def effect_event(
    attempt: int, eid: str, call: str = 'call-charge-1', replayed: bool = False, key: str | None = None
) -> dict[str, Any]:
    return {
        'attempt': attempt,
        'seq': 1,
        't_ms': 1.0,
        'event': 'effect_committed',
        'tool': 'charge_order',
        'tool_call_id': call,
        'effect_id': eid,
        'replayed': replayed,
        'idempotency_key': key,
    }


KILL = {'attempt': 1, 'seq': 2, 'event': 'kill', 'point': 'after_effect', 'crash': 'sigkill'}


def facts(**kw: Any) -> CellFacts:
    base: dict[str, Any] = {
        'cell': CellConfig(kill='after_effect'),
        'kill_event': KILL,
        'completed': True,
        'final_output': 'DONE',
        'resume': {'mechanism': 'history', 'history_source': 'history.json', 'history_len': 2, 'history_tail': []},
    }
    base.update(kw)
    return CellFacts(**base)


def ret(content: str, outcome: str = 'success', synthesized: bool = False) -> dict[str, Any]:
    return {
        'tool': 'charge_order',
        'tool_call_id': 'call-charge-1',
        'outcome': outcome,
        'content': content,
        'synthesized': synthesized,
    }


def test_ok() -> None:
    f = facts(
        effects={'charge_order': [charge(1, 1)]},
        timeline=[effect_event(1, 'ch_0001'), KILL],
        final_returns=[ret('charged ch_0001 (reconciled from provider)')],
    )
    c = classify(f)
    assert c.verdict == 'OK'
    assert c.effect_counts == {'charge_order': 1}
    assert all(ch['status'] != 'fail' for ch in c.checks)


def test_duplicate_with_attribution() -> None:
    f = facts(
        effects={'charge_order': [charge(1, 1), charge(2, 2)]},
        timeline=[effect_event(1, 'ch_0001'), KILL, effect_event(2, 'ch_0002')],
        final_returns=[ret('charged ch_0002')],
    )
    c = classify(f)
    assert c.verdict == 'DUPLICATE'
    cats = {r['category'] for r in c.responsibility}
    assert {'framework_documented', 'application_choice'} <= cats
    assert any(ch['name'] == 'transcript_vs_provider' and ch['status'] == 'fail' for ch in c.checks)
    assert 'Verdict: DUPLICATE' in c.explanation
    assert 'ch_0002' in c.explanation


def test_duplicate_beats_stranded() -> None:
    f = facts(effects={'charge_order': [charge(1, 1), charge(2, 2)]}, completed=False, final_error='boom')
    assert classify(f).verdict == 'DUPLICATE'


def test_stranded() -> None:
    f = facts(effects={'charge_order': [charge(1, 1)]}, completed=False, final_error='UserError: nope')
    c = classify(f)
    assert c.verdict == 'STRANDED'
    assert 'nope' in c.explanation


def test_missing() -> None:
    f = facts(effects={'charge_order': []}, final_returns=[ret('x', outcome='interrupted', synthesized=True)])
    assert classify(f).verdict == 'MISSING'


def test_ambiguous_resolved_when_closed_as_interrupted_but_charged() -> None:
    f = facts(
        effects={'charge_order': [charge(1, 1)]},
        timeline=[effect_event(1, 'ch_0001'), KILL],
        final_returns=[ret('The tool call was interrupted', outcome='interrupted', synthesized=True)],
    )
    c = classify(f)
    assert c.verdict == 'AMBIGUOUS_RESOLVED'
    assert any(ch['name'].startswith('interrupted_but_effect_exists') for ch in c.checks)
    assert any('synthesized' in r['factor'] for r in c.responsibility)


def test_kill_not_reached() -> None:
    f = facts(kill_event=None, resume=None, effects={'charge_order': [charge(1, 1)]})
    assert classify(f).verdict == 'KILL_NOT_REACHED'


def test_not_applicable_and_lab_error_take_precedence() -> None:
    assert classify(facts(na_reason='x')).verdict == 'NOT_APPLICABLE'
    assert classify(facts(lab_error='y', effects={'charge_order': [charge(1, 1), charge(2, 1)]})).verdict == 'LAB_ERROR'


def test_replayed_effect_is_a_provider_assumption() -> None:
    f = facts(
        cell=CellConfig(kill='after_effect', key='business'),
        effects={'charge_order': [charge(1, 1, key='order:order-1001:charge')]},
        timeline=[
            effect_event(1, 'ch_0001', key='order:order-1001:charge'),
            KILL,
            effect_event(2, 'ch_0001', replayed=True, key='order:order-1001:charge'),
        ],
        final_returns=[ret('charged ch_0001 (replayed by provider)')],
    )
    c = classify(f)
    assert c.verdict == 'OK'
    assert any(r['category'] == 'provider_assumption' for r in c.responsibility)


def test_key_change_is_reported() -> None:
    f = facts(
        cell=CellConfig(kill='after_effect', key='run_id+tool_call_id'),
        effects={'charge_order': [charge(1, 1, key='r1:c'), charge(2, 2, key='r2:c')]},
        timeline=[effect_event(1, 'ch_0001', key='r1:c'), KILL, effect_event(2, 'ch_0002', key='r2:c')],
    )
    c = classify(f)
    assert any('different key on resume' in r['factor'] for r in c.responsibility)


def test_restart_with_same_stable_id_is_not_called_a_reexecution() -> None:
    f = facts(
        resume={'mechanism': 'restart', 'history_source': 'none', 'fallback_restart': False},
        effects={'charge_order': [charge(1, 1), charge(2, 2)]},
        timeline=[effect_event(1, 'ch_0001'), KILL, effect_event(2, 'ch_0002')],
    )
    factors = [r['factor'] for r in classify(f).responsibility]
    assert not any('executes them' in x for x in factors)
    assert any('asked the model again' in x for x in factors)
    assert any('only because ids=stable' in x for x in factors)


def test_ledger_checks_use_attempt_one_state_only() -> None:
    f = facts(
        cell=CellConfig(persistence='step_frontier', resume='continue_interrupted', kill='after_effect'),
        effects={'charge_order': [charge(1, 1, call='c1'), charge(2, 2, call='c2')]},
        ledger=[
            {'run_id': 'crashlab-run-a1', 'tool_call_id': 'c1', 'status': 'started'},
            {'run_id': 'crashlab-run-a1', 'tool_call_id': 'c2', 'status': 'completed'},
        ],
        snapshots=[{'run_id': 'crashlab-run-a2', 'return_ids': ['c2']}],
    )
    checks = {ch['name']: ch for ch in classify(f).checks}
    assert 'committed a FakePay effect' in checks['ledger_vs_provider[c1]']['detail']
    # c2's effect was made by attempt 2, so attempt 1's "completed" record has no attempt-1 effect.
    assert checks['ledger_vs_provider[c2]']['status'] == 'fail'


def test_fingerprint_ignores_volatile_fields() -> None:
    a = [{'attempt': 1, 'seq': 1, 't_ms': 1.0, 'event': 'tool_started', 'run_id': 'uuid-1', 'tool': 't'}]
    b = [{'attempt': 1, 'seq': 1, 't_ms': 9.9, 'event': 'tool_started', 'run_id': 'uuid-2', 'tool': 't'}]
    assert fingerprint('OK', {'x': 1}, a) == fingerprint('OK', {'x': 1}, b)
    assert fingerprint('OK', {'x': 1}, a) != fingerprint('DUPLICATE', {'x': 1}, a)
    k1 = [{'event': 'effect_committed', 'idempotency_key': 'r1:t1'}]
    k2 = [{'event': 'effect_committed', 'idempotency_key': 'r2:t2'}]
    k0 = [{'event': 'effect_committed', 'idempotency_key': None}]
    assert fingerprint('OK', {}, k1) == fingerprint('OK', {}, k2) != fingerprint('OK', {}, k0)


def test_failed_ledger_record_with_an_effect_is_reported() -> None:
    f = facts(
        cell=CellConfig(persistence='step', resume='continue_interrupted', kill='after_effect', crash='exception'),
        effects={'charge_order': [charge(1, 1, call='c1')]},
        ledger=[{'run_id': 'crashlab-run-a1', 'tool_call_id': 'c1', 'status': 'failed'}],
    )
    checks = {ch['name']: ch for ch in classify(f).checks}
    assert 'ledger says failed' in checks['ledger_vs_provider[c1]']['detail']
