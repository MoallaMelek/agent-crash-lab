from __future__ import annotations

import pytest

from crashlab.config import CellConfig, ChildConfig, not_applicable_reason


def test_defaults_are_valid_and_applicable() -> None:
    cfg = CellConfig()
    cfg.validate()
    assert not_applicable_reason(cfg) is None
    assert cfg.expected_effects() == {'charge_order': 1}


def test_unknown_axis_value_is_rejected() -> None:
    with pytest.raises(ValueError, match='persistence'):
        CellConfig.from_dict({'persistence': 'magic'})
    with pytest.raises(ValueError, match='unknown cell config keys'):
        CellConfig.from_dict({'nope': 1})


@pytest.mark.parametrize(
    ('cfg', 'fragment'),
    [
        (CellConfig(persistence='iter_naive', resume='continue_default'), 'StepPersistence'),
        (CellConfig(persistence='step', resume='history'), 'continue_run'),
        (CellConfig(persistence='iter_folded', kill='during_snapshot_write'), 'store'),
        (CellConfig(kill='mid_parallel_batch'), 'parallel_batch'),
        (CellConfig(kill='after_tool_node', crash='exception'), 'tool-body'),
        (CellConfig(crash='exception'), 'without a kill point'),
    ],
)
def test_not_applicable_rules(cfg: CellConfig, fragment: str) -> None:
    reason = not_applicable_reason(cfg)
    assert reason is not None
    assert fragment in reason


def test_two_effect_scenarios_expect_a_notification() -> None:
    assert CellConfig(scenario='parallel_batch').expected_effects() == {'charge_order': 1, 'notify_customer': 1}


def test_child_config_round_trip() -> None:
    c = ChildConfig(cell=CellConfig(kill='after_effect').to_dict(), attempt=2, workdir='/tmp/x', action='resume')
    assert ChildConfig.from_json(c.to_json()) == c
    assert 'kill=after_effect' in CellConfig(kill='after_effect').label()
