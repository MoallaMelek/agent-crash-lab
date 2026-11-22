"""Cell configuration: the axes of the matrix and which combinations are meaningful.

A *cell* is one fully specified experiment: a scenario, a persistence pattern, a resume
mechanism, an effect-key strategy, model behaviour, and a fault (kill point + crash mode).
`not_applicable_reason` encodes the honest N/A rules: a combination that cannot be built
without faking a mechanism is reported as `NOT_APPLICABLE`, never silently substituted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal, get_args

Scenario = Literal['single_charge', 'charge_then_notify', 'parallel_batch']
Persistence = Literal['final_only', 'iter_naive', 'iter_folded', 'step', 'step_frontier']
Resume = Literal['restart', 'history', 'continue_default', 'continue_interrupted', 'repair', 'reconcile']
KeyStrategy = Literal['none', 'run_id+tool_call_id', 'tool_call_id', 'response_origin', 'business']
IdMode = Literal['stable', 'fresh']
AfterInterrupted = Literal['trust', 'reissue']
KillPoint = Literal[
    'none',
    'before_effect',
    'after_effect',
    'after_tool_node',
    'during_next_model_request',
    'mid_parallel_batch',
    'during_snapshot_write',
    'after_snapshot_write',
]
CrashMode = Literal['sigkill', 'exception']

SCENARIOS: tuple[Scenario, ...] = get_args(Scenario)
PERSISTENCE: tuple[Persistence, ...] = get_args(Persistence)
RESUMES: tuple[Resume, ...] = get_args(Resume)
KEY_STRATEGIES: tuple[KeyStrategy, ...] = get_args(KeyStrategy)
ID_MODES: tuple[IdMode, ...] = get_args(IdMode)
AFTER_INTERRUPTED: tuple[AfterInterrupted, ...] = get_args(AfterInterrupted)
KILL_POINTS: tuple[KillPoint, ...] = get_args(KillPoint)
CRASH_MODES: tuple[CrashMode, ...] = get_args(CrashMode)

STEP_PATTERNS: frozenset[str] = frozenset({'step', 'step_frontier'})
APP_HISTORY_PATTERNS: frozenset[str] = frozenset({'final_only', 'iter_naive', 'iter_folded'})

CHARGE_TOOL = 'charge_order'
NOTIFY_TOOL = 'notify_customer'
ORDER_ID = 'order-1001'
AMOUNT_CENTS = 4200
PROMPT = f'Charge order {ORDER_ID} for {AMOUNT_CENTS} cents.'
CONTINUE_PROMPT = 'The previous process stopped unexpectedly. Continue the task.'

# StepPersistence run ids are explicit so attempt 2 can find attempt 1's records.
STEP_RUN_ID_TEMPLATE = 'crashlab-run-a{attempt}'

Verdict = Literal[
    'OK', 'DUPLICATE', 'MISSING', 'STRANDED', 'AMBIGUOUS_RESOLVED', 'KILL_NOT_REACHED', 'NOT_APPLICABLE', 'LAB_ERROR'
]
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)


@dataclass(frozen=True)
class CellConfig:
    """One experiment. Defaults describe the simplest single-charge agent."""

    scenario: Scenario = 'single_charge'
    persistence: Persistence = 'iter_naive'
    resume: Resume = 'history'
    key: KeyStrategy = 'none'
    ids: IdMode = 'stable'
    after_interrupted: AfterInterrupted = 'trust'
    kill: KillPoint = 'none'
    crash: CrashMode = 'sigkill'
    kill_tool: str = CHARGE_TOOL
    """Tool whose boundary the kill point refers to (for tool-relative kill points)."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CellConfig:
        names = {f.name for f in fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f'unknown cell config keys: {sorted(unknown)}')
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Raise `ValueError` for a value outside an axis (not for N/A combinations)."""
        checks: list[tuple[str, object, tuple[str, ...]]] = [
            ('scenario', self.scenario, SCENARIOS),
            ('persistence', self.persistence, PERSISTENCE),
            ('resume', self.resume, RESUMES),
            ('key', self.key, KEY_STRATEGIES),
            ('ids', self.ids, ID_MODES),
            ('after_interrupted', self.after_interrupted, AFTER_INTERRUPTED),
            ('kill', self.kill, KILL_POINTS),
            ('crash', self.crash, CRASH_MODES),
            ('kill_tool', self.kill_tool, (CHARGE_TOOL, NOTIFY_TOOL)),
        ]
        for name, value, allowed in checks:
            if value not in allowed:
                raise ValueError(f'{name}={value!r} is not one of {list(allowed)}')

    @property
    def uses_step_persistence(self) -> bool:
        return self.persistence in STEP_PATTERNS

    @property
    def expects_crash(self) -> bool:
        return self.kill != 'none'

    def expected_effects(self) -> dict[str, int]:
        """Effects the task should produce exactly once: tool name -> 1."""
        if self.scenario == 'single_charge':
            return {CHARGE_TOOL: 1}
        return {CHARGE_TOOL: 1, NOTIFY_TOOL: 1}

    def label(self) -> str:
        parts = [
            self.scenario,
            self.persistence,
            f'resume={self.resume}',
            f'key={self.key}',
            f'ids={self.ids}',
            f'model={self.after_interrupted}',
            f'kill={self.kill}',
        ]
        if self.crash != 'sigkill':
            parts.append(f'crash={self.crash}')
        if self.kill_tool != CHARGE_TOOL:
            parts.append(f'kill_tool={self.kill_tool}')
        return ' '.join(parts)


@dataclass(frozen=True)
class NotApplicable:
    reason: str


def not_applicable_reason(cfg: CellConfig) -> str | None:
    """Why a combination cannot be run honestly, or None when it can.

    These rules are the only place a combination is excluded; the runner never substitutes
    one mechanism for another to make a cell "work".
    """
    step = cfg.uses_step_persistence
    if cfg.resume in ('continue_default', 'continue_interrupted') and not step:
        return (
            f'{cfg.resume} is the harness `continue_run` read path, which needs a StepPersistence store; '
            f'persistence={cfg.persistence} has none.'
        )
    if cfg.resume == 'history' and step:
        return (
            'With StepPersistence the persisted history is read through `continue_run`; '
            'use resume=continue_default or continue_interrupted instead of a separate history file.'
        )
    if cfg.kill in ('during_snapshot_write', 'after_snapshot_write') and not step:
        return f'{cfg.kill} wraps the StepPersistence store; persistence={cfg.persistence} has no store.'
    if cfg.kill == 'mid_parallel_batch' and cfg.scenario != 'parallel_batch':
        return 'mid_parallel_batch needs two tool calls in one ModelResponse (scenario=parallel_batch).'
    if cfg.kill == 'mid_parallel_batch' and cfg.kill_tool != CHARGE_TOOL:
        return 'mid_parallel_batch is defined as: charge settled, notification in flight.'
    if cfg.kill == 'none' and cfg.crash != 'sigkill':
        return 'crash mode is meaningless without a kill point.'
    if cfg.crash == 'exception' and cfg.kill not in ('before_effect', 'after_effect'):
        return 'crash=exception is only defined for the tool-body kill points (before_effect, after_effect).'
    # kill_tool=notify_customer with scenario=single_charge is deliberately allowed: the kill
    # point can never fire, which is how the KILL_NOT_REACHED self-check cell is built.
    return None


@dataclass
class ChildConfig:
    """What the parent passes to one child attempt (serialised as JSON)."""

    cell: dict[str, Any]
    attempt: int
    workdir: str
    action: Literal['first_run', 'resume']
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> ChildConfig:
        data = json.loads(text)
        return cls(**data)
