from __future__ import annotations

from pathlib import Path

from crashlab.provider import FakePay


def test_charge_without_key_always_creates(fakepay: FakePay) -> None:
    a = fakepay.charge('o1', 100)
    b = fakepay.charge('o1', 100)
    assert a.charge_id != b.charge_id
    assert not a.replayed
    assert not b.replayed
    assert fakepay.count('o1') == 2


def test_same_key_returns_original_and_flags_replay(fakepay: FakePay) -> None:
    first = fakepay.charge('o1', 100, 'k1', call_ref='call-1')
    again = fakepay.charge('o1', 100, 'k1', call_ref='call-2')
    assert again.replayed
    assert again.charge_id == first.charge_id
    assert again.call_ref == 'call-1', 'the original row is returned, not the retry'
    assert fakepay.count('o1') == 1
    kinds = [e['kind'] for e in fakepay.dump()['events']]
    assert kinds == ['charge_created', 'charge_replayed']


def test_different_keys_are_different_charges(fakepay: FakePay) -> None:
    fakepay.charge('o1', 100, 'k1')
    fakepay.charge('o1', 100, 'k2')
    assert fakepay.count('o1') == 2


def test_lookups(fakepay: FakePay) -> None:
    c = fakepay.charge('o1', 100, 'k1', call_ref='call-1')
    fakepay.charge('o2', 5)
    found = fakepay.find_by_key('k1')
    assert found is not None
    assert found.charge_id == c.charge_id
    assert fakepay.find_by_key('nope') is None
    by_ref = fakepay.find_by_call_ref('call-1')
    assert by_ref is not None
    assert by_ref.charge_id == c.charge_id
    assert fakepay.find_by_call_ref('call-x') is None
    assert [r.charge_id for r in fakepay.find_by_order('o1')] == [c.charge_id]
    assert fakepay.count('missing') == 0


def test_committed_rows_visible_to_a_new_client(tmp_path: Path) -> None:
    FakePay(tmp_path / 'p.db', attempt=1).charge('o1', 1, 'k')
    other = FakePay(tmp_path / 'p.db', attempt=2)
    again = other.charge('o1', 1, 'k')
    assert again.replayed
    assert again.attempt == 1
    assert other.count('o1') == 1


def test_notifications_share_key_semantics(fakepay: FakePay) -> None:
    n1 = fakepay.notify('o1', 'hi', 'nk', call_ref='call-n')
    n2 = fakepay.notify('o1', 'hi', 'nk')
    fakepay.notify('o1', 'hi')
    assert n2.replayed
    assert n2.notification_id == n1.notification_id
    assert fakepay.count_notifications('o1') == 2
    assert fakepay.find_notification_by_key('nk') is not None
    assert fakepay.find_notification_by_call_ref('call-n') is not None
    assert fakepay.find_notification_by_key('zz') is None


def test_ids_are_sequential_and_dump_is_complete(fakepay: FakePay) -> None:
    fakepay.charge('o1', 1)
    fakepay.charge('o1', 1)
    fakepay.notify('o1', 'm')
    dump = fakepay.dump()
    assert [r['id'] for r in dump['charges']] == ['ch_0001', 'ch_0002']
    assert [r['id'] for r in dump['notifications']] == ['nt_0001']
    assert len(dump['events']) == 3
    assert all(r['attempt'] == 1 for r in dump['charges'])
