"""The curated crash matrix, the optional full product, and the parallel/repeat driver.

Every curated cell tests one claim and declares the verdict the research predicts
(`docs/ENGINEERING_DECISIONS.md` section 1). `matrix --check` fails when an observed verdict
differs, so a Pydantic AI upgrade that changes behaviour shows up in CI.

Findings (section 1 of ENGINEERING_DECISIONS.md):

* `F1`: saving `all_messages()` per `iter` node leaves a window as long as the next model request.
* `F2`: a `tool_call_id` key only works on resume paths that keep the persisted `ModelResponse`.
* `F3`: a key built on `ctx.run_id` breaks on every resume that re-executes the call.
* `F4`: after a synthesized `interrupted` return, a model may re-issue the call under a new id.
* `F5`: `DeferredToolResults(calls=...)` resolves a never-deferred dangling call (reconciliation hook).

Other groups: `H` headline demo, `B` no-crash baselines, `R` restart, `D` documented framework
behaviour and honest N/A handling, `S` lab self-checks.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from crashlab.config import (
    AFTER_INTERRUPTED,
    ID_MODES,
    KEY_STRATEGIES,
    KILL_POINTS,
    PERSISTENCE,
    RESUMES,
    SCENARIOS,
    CellConfig,
    not_applicable_reason,
)
from crashlab.runner import run_cell

FINDINGS = {
    'H': 'Headline demo',
    'B': 'Baselines without a crash',
    'F1': 'Per-node all_messages() persistence loses tool returns during the next model request',
    'F2': 'tool_call_id keys break on resume paths that re-ask the model',
    'F3': 'run_id-based keys break on every resume that re-executes the call',
    'F4': 'A model may re-issue a call after a synthesized interrupted return',
    'F5': 'DeferredToolResults(calls=...) works as a reconciliation hook for never-deferred calls',
    'R': 'Restart without persisted state',
    'D': 'Documented framework behaviour and honest N/A handling',
    'S': 'Lab self-checks',
}


@dataclass(frozen=True)
class CuratedCell:
    id: str
    finding: str
    claim: str
    expected: str
    config: CellConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'finding': self.finding,
            'claim': self.claim,
            'expected': self.expected,
            'config': self.config.to_dict(),
        }


def _c(id: str, finding: str, expected: str, claim: str, **cfg: Any) -> CuratedCell:
    config = CellConfig(**cfg)
    config.validate()
    return CuratedCell(id=id, finding=finding, claim=claim, expected=expected, config=config)


CURATED: tuple[CuratedCell, ...] = (
    # ------------------------------------------------------------------ headline
    _c('H1-no-crash', 'H', 'OK', 'Without a crash the agent charges exactly once.', persistence='iter_naive'),
    _c(
        'H2-naive-history-duplicates',
        'H',
        'DUPLICATE',
        'A SIGKILL after the charge, then resuming the per-node history with no prompt, charges twice.',
        persistence='iter_naive',
        kill='after_effect',
        resume='history',
    ),
    _c(
        'H3-business-key-dedupes',
        'H',
        'OK',
        'The same crash and resume with a business idempotency key gives one charge.',
        persistence='iter_naive',
        kill='after_effect',
        resume='history',
        key='business',
    ),
    _c(
        'H4-reconcile-dedupes',
        'H',
        'OK',
        'The same crash with reconciliation against the provider gives one charge without an idempotency key '
        '(the lookup uses the tool_call_id the tool stored as provider metadata).',
        persistence='iter_naive',
        kill='after_effect',
        resume='reconcile',
    ),
    # ----------------------------------------------------------------- baselines
    *(
        _c(
            f'B-{p}-{s}',
            'B',
            'OK',
            f'Baseline: {s} with {p} persistence and no crash produces each effect once.',
            scenario=s,
            persistence=p,
            resume='continue_default' if p.startswith('step') else 'history',
        )
        for p, s in [
            ('final_only', 'single_charge'),
            ('iter_naive', 'single_charge'),
            ('iter_folded', 'single_charge'),
            ('step', 'single_charge'),
            ('step_frontier', 'single_charge'),
            ('iter_naive', 'charge_then_notify'),
            ('step', 'charge_then_notify'),
            ('iter_naive', 'parallel_batch'),
            ('step_frontier', 'parallel_batch'),
        ]
    ),
    # ----------------------------------------------------------------------- F1
    _c(
        'F1-iter-naive-next-request',
        'F1',
        'DUPLICATE',
        'iter_naive loses the tool return during the next model request: the resumed history still ends with the call.',
        persistence='iter_naive',
        kill='during_next_model_request',
    ),
    _c(
        'F1-iter-folded-next-request',
        'F1',
        'OK',
        'Folding node.request into the saved history closes the next-model-request window.',
        persistence='iter_folded',
        kill='during_next_model_request',
    ),
    _c(
        'F1-step-next-request',
        'F1',
        'OK',
        'StepPersistence folds the pending tool-return request into its settled snapshot (harness #373).',
        persistence='step',
        kill='during_next_model_request',
        resume='continue_default',
    ),
    _c(
        'F1-step-frontier-next-request',
        'F1',
        'OK',
        'With capture_frontier the settled snapshot also carries the tool return.',
        persistence='step_frontier',
        kill='during_next_model_request',
        resume='continue_default',
    ),
    _c(
        'F1-final-only-next-request',
        'F1',
        'DUPLICATE',
        'Saving only at the end leaves nothing to resume; a restart charges again.',
        persistence='final_only',
        kill='during_next_model_request',
        resume='history',
    ),
    _c(
        'F1-iter-naive-after-tool-node',
        'F1',
        'DUPLICATE',
        'The window opens as soon as the CallToolsNode finishes, before the app saves.',
        persistence='iter_naive',
        kill='after_tool_node',
    ),
    _c(
        'F1-iter-folded-after-tool-node',
        'F1',
        'DUPLICATE',
        'Folding does not help when the kill lands before the save itself.',
        persistence='iter_folded',
        kill='after_tool_node',
    ),
    _c(
        'F1-iter-naive-next-request-business',
        'F1',
        'OK',
        'A business key makes the iter_naive window harmless for this effect.',
        persistence='iter_naive',
        kill='during_next_model_request',
        key='business',
    ),
    _c(
        'F1-iter-naive-next-request-reconcile',
        'F1',
        'OK',
        'Reconciliation also covers the iter_naive window: the dangling call is found in the provider.',
        persistence='iter_naive',
        kill='during_next_model_request',
        resume='reconcile',
    ),
    _c(
        'F1-notify-iter-naive-next-request',
        'F1',
        'DUPLICATE',
        'In a two-step task the charge is repeated when the kill lands in the request after it.',
        scenario='charge_then_notify',
        persistence='iter_naive',
        kill='during_next_model_request',
    ),
    _c(
        'F1-notify-iter-folded-next-request',
        'F1',
        'OK',
        'The folded pattern keeps the charge return in a two-step task.',
        scenario='charge_then_notify',
        persistence='iter_folded',
        kill='during_next_model_request',
    ),
    _c(
        'F1-step-after-tool-node',
        'F1',
        'DUPLICATE',
        'A kill after the first CallToolsNode but before StepPersistence saves leaves no complete snapshot.',
        persistence='step',
        kill='after_tool_node',
        resume='continue_default',
    ),
    _c(
        'F1-step-during-snapshot-write',
        'F1',
        'DUPLICATE',
        'A kill entering the settled snapshot write is equivalent: no complete snapshot, restart re-charges.',
        persistence='step',
        kill='during_snapshot_write',
        resume='continue_default',
    ),
    _c(
        'F1-step-after-snapshot-write',
        'F1',
        'OK',
        'Once the settled snapshot has committed, continue_run resumes past the charge.',
        persistence='step',
        kill='after_snapshot_write',
        resume='continue_default',
    ),
    _c(
        'F1-step-frontier-during-snapshot-write-interrupted',
        'F1',
        'DUPLICATE',
        'With capture_frontier the interrupted frontier still ends at the call, so continue_interrupted re-runs it.',
        persistence='step_frontier',
        kill='during_snapshot_write',
        resume='continue_interrupted',
    ),
    # ----------------------------------------------------------------------- F2
    _c(
        'F2-iter-naive-history-tcid-fresh',
        'F2',
        'OK',
        'A tool_call_id key works when the resume keeps the persisted ModelResponse, even with fresh ids.',
        persistence='iter_naive',
        kill='after_effect',
        key='tool_call_id',
        ids='fresh',
    ),
    _c(
        'F2-step-default-tcid-fresh',
        'F2',
        'DUPLICATE',
        'StepPersistence default: no snapshot exists in the first cycle, the app restarts, the model mints a new id.',
        persistence='step',
        kill='after_effect',
        resume='continue_default',
        key='tool_call_id',
        ids='fresh',
    ),
    _c(
        'F2-step-default-tcid-stable',
        'F2',
        'OK',
        'The same path with stable ids passes only because the re-asked model repeats the id (a test artefact).',
        persistence='step',
        kill='after_effect',
        resume='continue_default',
        key='tool_call_id',
        ids='stable',
    ),
    _c(
        'F2-step-frontier-default-tcid-fresh',
        'F2',
        'DUPLICATE',
        'capture_frontier: the newest complete snapshot is the prompt only, so continue_default re-asks the model.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_default',
        key='tool_call_id',
        ids='fresh',
    ),
    _c(
        'F2-step-frontier-default-tcid-stable',
        'F2',
        'OK',
        'Stable ids mask the same defect.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_default',
        key='tool_call_id',
        ids='stable',
    ),
    _c(
        'F2-step-frontier-interrupted-tcid-fresh',
        'F2',
        'OK',
        'include_interrupted=True keeps the dangling response, so the tool_call_id key holds.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_interrupted',
        key='tool_call_id',
        ids='fresh',
    ),
    _c(
        'F2-restart-tcid-fresh',
        'F2',
        'DUPLICATE',
        'A restart re-asks the model; with fresh ids the tool_call_id key changes.',
        persistence='iter_naive',
        kill='after_effect',
        resume='restart',
        key='tool_call_id',
        ids='fresh',
    ),
    _c(
        'F2-final-only-tcid-fresh',
        'F2',
        'DUPLICATE',
        'final_only has nothing to resume from, so the model is re-asked and the key changes.',
        persistence='final_only',
        kill='after_effect',
        resume='history',
        key='tool_call_id',
        ids='fresh',
    ),
    _c(
        'F2-step-frontier-default-origin-stable',
        'F2',
        'DUPLICATE',
        'A response-origin key also breaks when the model is re-asked: the new response has a new timestamp.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_default',
        key='response_origin',
        ids='stable',
    ),
    _c(
        'F2-step-frontier-interrupted-origin-fresh',
        'F2',
        'OK',
        'A response-origin key holds when the persisted response is kept.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_interrupted',
        key='response_origin',
        ids='fresh',
    ),
    _c(
        'F2-step-default-business-fresh',
        'F2',
        'OK',
        'A business key survives the restart fallback, whatever ids the model mints.',
        persistence='step',
        kill='after_effect',
        resume='continue_default',
        key='business',
        ids='fresh',
    ),
    _c(
        'F2-step-frontier-default-business-fresh',
        'F2',
        'OK',
        'A business key survives continue_default re-asking the model.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_default',
        key='business',
        ids='fresh',
    ),
    _c(
        'F2-final-only-business-fresh',
        'F2',
        'OK',
        'A business key survives a full restart.',
        persistence='final_only',
        kill='after_effect',
        resume='restart',
        key='business',
        ids='fresh',
    ),
    _c(
        'F2-parallel-step-default-tcid-fresh',
        'F2',
        'DUPLICATE',
        'Mid-batch kill under StepPersistence default: restart with new ids repeats both effects.',
        scenario='parallel_batch',
        persistence='step',
        kill='mid_parallel_batch',
        resume='continue_default',
        key='tool_call_id',
        ids='fresh',
    ),
    # ----------------------------------------------------------------------- F3
    _c(
        'F3-iter-naive-history-runid',
        'F3',
        'DUPLICATE',
        'A run_id+tool_call_id key changes on resume because every Agent.run gets a new run_id.',
        persistence='iter_naive',
        kill='after_effect',
        key='run_id+tool_call_id',
    ),
    _c(
        'F3-iter-folded-history-runid',
        'F3',
        'DUPLICATE',
        'The folded pattern does not help a run_id-based key.',
        persistence='iter_folded',
        kill='after_effect',
        key='run_id+tool_call_id',
    ),
    _c(
        'F3-iter-naive-next-request-runid',
        'F3',
        'DUPLICATE',
        'The run_id key also fails in the iter_naive next-request window.',
        persistence='iter_naive',
        kill='during_next_model_request',
        key='run_id+tool_call_id',
    ),
    _c(
        'F3-step-frontier-interrupted-runid',
        'F3',
        'DUPLICATE',
        'Even the resume path that keeps the persisted response breaks a run_id-based key.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_interrupted',
        key='run_id+tool_call_id',
    ),
    _c(
        'F3-iter-naive-history-tcid',
        'F3',
        'OK',
        'Contrast: tool_call_id alone survives the same resume.',
        persistence='iter_naive',
        kill='after_effect',
        key='tool_call_id',
    ),
    _c(
        'F3-iter-naive-history-origin',
        'F3',
        'OK',
        'Contrast: the response-origin key (ModelResponse.run_id + timestamp, #11960) survives the same resume.',
        persistence='iter_naive',
        kill='after_effect',
        key='response_origin',
    ),
    _c(
        'F3-step-frontier-interrupted-origin',
        'F3',
        'OK',
        'Contrast: response-origin key under StepPersistence continue_interrupted.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_interrupted',
        key='response_origin',
    ),
    _c(
        'F3-iter-naive-reconcile-runid',
        'F3',
        'OK',
        'Reconciliation recovers the original run_id from ModelResponse.run_id and finds the charge.',
        persistence='iter_naive',
        kill='after_effect',
        resume='reconcile',
        key='run_id+tool_call_id',
    ),
    # ----------------------------------------------------------------------- F4
    _c(
        'F4-repair-trust',
        'F4',
        'AMBIGUOUS_RESOLVED',
        'repair_messages closes the call as interrupted; a trusting model ends with the charge unacknowledged.',
        persistence='iter_naive',
        kill='after_effect',
        resume='repair',
        after_interrupted='trust',
    ),
    _c(
        'F4-repair-reissue',
        'F4',
        'DUPLICATE',
        'A model that re-issues after the interrupted return charges again under a new id.',
        persistence='iter_naive',
        kill='after_effect',
        resume='repair',
        after_interrupted='reissue',
    ),
    _c(
        'F4-repair-reissue-tcid',
        'F4',
        'DUPLICATE',
        'A tool_call_id key cannot catch the re-issued call: it has a new id.',
        persistence='iter_naive',
        kill='after_effect',
        resume='repair',
        after_interrupted='reissue',
        key='tool_call_id',
    ),
    _c(
        'F4-repair-reissue-origin',
        'F4',
        'DUPLICATE',
        'A response-origin key cannot catch it either: it comes from a new response.',
        persistence='iter_naive',
        kill='after_effect',
        resume='repair',
        after_interrupted='reissue',
        key='response_origin',
    ),
    _c(
        'F4-repair-reissue-business',
        'F4',
        'OK',
        'Only a key derived from the business operation dedupes the re-issued call.',
        persistence='iter_naive',
        kill='after_effect',
        resume='repair',
        after_interrupted='reissue',
        key='business',
    ),
    _c(
        'F4-repair-before-effect-trust',
        'F4',
        'MISSING',
        'Killed before the charge, repair + a trusting model completes the run with no charge.',
        persistence='iter_naive',
        kill='before_effect',
        resume='repair',
        after_interrupted='trust',
    ),
    _c(
        'F4-repair-before-effect-reissue',
        'F4',
        'OK',
        'Killed before the charge, a re-issuing model charges once.',
        persistence='iter_naive',
        kill='before_effect',
        resume='repair',
        after_interrupted='reissue',
    ),
    _c(
        'F4-step-frontier-repair-reissue',
        'F4',
        'DUPLICATE',
        'Same model re-issue under StepPersistence (repair of the interrupted frontier).',
        persistence='step_frontier',
        kill='after_effect',
        resume='repair',
        after_interrupted='reissue',
    ),
    _c(
        'F4-exception-final-only-trust',
        'F4',
        'AMBIGUOUS_RESOLVED',
        'In-process exception: the documented capture_run_messages resume does not re-run the tool, '
        'but the transcript says interrupted while the charge exists.',
        persistence='final_only',
        kill='after_effect',
        crash='exception',
        resume='history',
        after_interrupted='trust',
    ),
    _c(
        'F4-exception-final-only-reissue',
        'F4',
        'DUPLICATE',
        'In-process exception, documented resume: a re-issuing model charges again (exp5).',
        persistence='final_only',
        kill='after_effect',
        crash='exception',
        resume='history',
        after_interrupted='reissue',
    ),
    _c(
        'F4-exception-final-only-reissue-business',
        'F4',
        'OK',
        'A business key dedupes the re-issued call after an in-process exception.',
        persistence='final_only',
        kill='after_effect',
        crash='exception',
        resume='history',
        after_interrupted='reissue',
        key='business',
    ),
    _c(
        'F4-exception-step-interrupted-trust',
        'F4',
        'AMBIGUOUS_RESOLVED',
        'StepPersistence on_run_error saves an interrupted snapshot; resuming it closes the call as interrupted.',
        persistence='step',
        kill='after_effect',
        crash='exception',
        resume='continue_interrupted',
        after_interrupted='trust',
    ),
    _c(
        'F4-exception-step-interrupted-reissue',
        'F4',
        'DUPLICATE',
        'The same StepPersistence path with a re-issuing model charges twice.',
        persistence='step',
        kill='after_effect',
        crash='exception',
        resume='continue_interrupted',
        after_interrupted='reissue',
    ),
    # ----------------------------------------------------------------------- F5
    _c(
        'F5-iter-naive-reconcile-before-effect',
        'F5',
        'OK',
        'Reconciliation finds nothing for a call killed before its effect and lets it execute.',
        persistence='iter_naive',
        kill='before_effect',
        resume='reconcile',
    ),
    _c(
        'F5-iter-folded-reconcile-after-effect',
        'F5',
        'OK',
        'Reconciliation injects the provider result for the dangling call (folded pattern).',
        persistence='iter_folded',
        kill='after_effect',
        resume='reconcile',
    ),
    _c(
        'F5-step-frontier-reconcile-after-effect',
        'F5',
        'OK',
        'Reconciliation from the interrupted StepPersistence frontier.',
        persistence='step_frontier',
        kill='after_effect',
        resume='reconcile',
    ),
    _c(
        'F5-step-frontier-reconcile-fresh',
        'F5',
        'OK',
        'Reconciliation is unaffected by fresh ids: it uses the persisted call.',
        persistence='step_frontier',
        kill='after_effect',
        resume='reconcile',
        ids='fresh',
    ),
    _c(
        'F5-step-default-reconcile-degraded',
        'F5',
        'DUPLICATE',
        'With StepPersistence defaults no snapshot carries the call; reconcile degrades to restart and records why.',
        persistence='step',
        kill='after_effect',
        resume='reconcile',
    ),
    _c(
        'F5-step-default-reconcile-degraded-business',
        'F5',
        'OK',
        'The degraded restart gives one charge only with a business key.',
        persistence='step',
        kill='after_effect',
        resume='reconcile',
        key='business',
    ),
    _c(
        'F5-final-only-reconcile-degraded',
        'F5',
        'DUPLICATE',
        'final_only gives reconciliation nothing to work with.',
        persistence='final_only',
        kill='after_effect',
        resume='reconcile',
    ),
    _c(
        'F5-notify-reconcile-after-charge',
        'F5',
        'OK',
        'Two-step task: reconcile the dangling charge, then the model goes on to notify.',
        scenario='charge_then_notify',
        persistence='iter_naive',
        kill='after_effect',
        resume='reconcile',
    ),
    _c(
        'F5-notify-history-after-notify',
        'F5',
        'DUPLICATE',
        'Two-step task killed after the notification: the settled charge is not repeated, the notification is.',
        scenario='charge_then_notify',
        persistence='iter_naive',
        kill='after_effect',
        kill_tool='notify_customer',
        resume='history',
    ),
    _c(
        'F5-notify-reconcile-after-notify',
        'F5',
        'OK',
        'Reconciliation covers the second effect too.',
        scenario='charge_then_notify',
        persistence='iter_naive',
        kill='after_effect',
        kill_tool='notify_customer',
        resume='reconcile',
    ),
    _c(
        'F5-parallel-iter-naive-history',
        'F5',
        'DUPLICATE',
        'Mid parallel batch: resuming the history re-runs the already-committed charge.',
        scenario='parallel_batch',
        persistence='iter_naive',
        kill='mid_parallel_batch',
        resume='history',
    ),
    _c(
        'F5-parallel-iter-naive-reconcile',
        'F5',
        'OK',
        'Partial reconciliation: inject the charge, approve the notification so it executes.',
        scenario='parallel_batch',
        persistence='iter_naive',
        kill='mid_parallel_batch',
        resume='reconcile',
    ),
    _c(
        'F5-parallel-iter-naive-business',
        'F5',
        'OK',
        'A business key also protects the committed half of the batch.',
        scenario='parallel_batch',
        persistence='iter_naive',
        kill='mid_parallel_batch',
        resume='history',
        key='business',
    ),
    _c(
        'F5-parallel-step-frontier-interrupted',
        'F5',
        'DUPLICATE',
        'The ledger says the charge completed, but no snapshot carries its result; continue_interrupted re-runs it.',
        scenario='parallel_batch',
        persistence='step_frontier',
        kill='mid_parallel_batch',
        resume='continue_interrupted',
    ),
    _c(
        'F5-parallel-step-frontier-reconcile',
        'F5',
        'OK',
        'Reconciling the interrupted frontier against the provider fixes the batch.',
        scenario='parallel_batch',
        persistence='step_frontier',
        kill='mid_parallel_batch',
        resume='reconcile',
    ),
    _c(
        'F5-parallel-step-default',
        'F5',
        'DUPLICATE',
        'StepPersistence defaults have no snapshot mid first batch; restart repeats the charge.',
        scenario='parallel_batch',
        persistence='step',
        kill='mid_parallel_batch',
        resume='continue_default',
    ),
    # ------------------------------------------------------------------------ R
    _c(
        'R-iter-naive-restart-after-effect',
        'R',
        'DUPLICATE',
        'Restarting the task after the charge charges again.',
        persistence='iter_naive',
        kill='after_effect',
        resume='restart',
    ),
    _c(
        'R-iter-naive-restart-before-effect',
        'R',
        'OK',
        'Restarting gives one charge when the crash came before the effect.',
        persistence='iter_naive',
        kill='before_effect',
        resume='restart',
    ),
    _c(
        'R-step-restart-after-snapshot',
        'R',
        'DUPLICATE',
        'Restarting ignores a perfectly good settled snapshot.',
        persistence='step',
        kill='after_snapshot_write',
        resume='restart',
    ),
    # ------------------------------------------------------------------------ D
    _c(
        'D-iter-naive-history-before-effect',
        'D',
        'OK',
        'A call killed before its effect is executed once on resume.',
        persistence='iter_naive',
        kill='before_effect',
    ),
    _c(
        'D-step-frontier-interrupted-before-effect',
        'D',
        'OK',
        'continue_interrupted re-runs a started call that had no effect yet.',
        persistence='step_frontier',
        kill='before_effect',
        resume='continue_interrupted',
    ),
    _c(
        'D-step-frontier-interrupted-after-effect',
        'D',
        'DUPLICATE',
        'continue_interrupted re-runs a started call; the ledger flags it as unknown_after_crash but nothing dedupes.',
        persistence='step_frontier',
        kill='after_effect',
        resume='continue_interrupted',
    ),
    _c(
        'D-step-continue-default-after-effect',
        'D',
        'DUPLICATE',
        'continue_default raises LookupError in the first cycle; the app restarts.',
        persistence='step',
        kill='after_effect',
        resume='continue_default',
    ),
    _c(
        'D-folded-history-next-request-answered',
        'D',
        'OK',
        'A persisted tool return is never re-run.',
        persistence='iter_folded',
        kill='during_next_model_request',
        key='none',
    ),
    _c(
        'D-na-continue-without-store',
        'D',
        'NOT_APPLICABLE',
        'continue_* needs a StepPersistence store; the lab refuses to fake it.',
        persistence='iter_naive',
        kill='after_effect',
        resume='continue_default',
    ),
    _c(
        'D-na-history-with-store',
        'D',
        'NOT_APPLICABLE',
        'StepPersistence history is read through continue_run, not a separate history file.',
        persistence='step',
        kill='after_effect',
        resume='history',
    ),
    _c(
        'D-na-snapshot-kill-without-store',
        'D',
        'NOT_APPLICABLE',
        'Snapshot-write kill points need a StepPersistence store.',
        persistence='iter_folded',
        kill='during_snapshot_write',
    ),
    _c(
        'D-na-parallel-kill-single-tool',
        'D',
        'NOT_APPLICABLE',
        'mid_parallel_batch needs two calls in one response.',
        scenario='single_charge',
        kill='mid_parallel_batch',
    ),
    # ------------------------------------------------------------------------ S
    _c(
        'S-kill-not-reached',
        'S',
        'KILL_NOT_REACHED',
        'The runner reports a configured kill point that never fired instead of passing silently.',
        scenario='single_charge',
        kill='after_effect',
        kill_tool='notify_customer',
    ),
)

HEADLINE_IDS = ('H1-no-crash', 'H2-naive-history-duplicates', 'H3-business-key-dedupes', 'H4-reconcile-dedupes')

QUICK_IDS = (
    *HEADLINE_IDS,
    'F1-iter-naive-next-request',
    'F1-iter-folded-next-request',
    'F2-step-default-tcid-fresh',
    'F2-step-frontier-interrupted-tcid-fresh',
    'F3-iter-naive-history-runid',
    'F4-repair-reissue',
    'F4-exception-final-only-trust',
    'F5-parallel-iter-naive-reconcile',
    'F5-parallel-step-frontier-interrupted',
    'D-na-continue-without-store',
    'S-kill-not-reached',
)


def curated_by_id() -> dict[str, CuratedCell]:
    out: dict[str, CuratedCell] = {}
    for c in CURATED:
        if c.id in out:
            raise ValueError(f'duplicate curated cell id {c.id}')
        out[c.id] = c
    return out


def full_product() -> list[CuratedCell]:
    """Every combination of the axes (sigkill only), minus N/A. Unchecked: no expected verdict."""
    cells: list[CuratedCell] = []
    for s, p, r, k, i, a, kp in itertools.product(
        SCENARIOS, PERSISTENCE, RESUMES, KEY_STRATEGIES, ID_MODES, AFTER_INTERRUPTED, KILL_POINTS
    ):
        cfg = CellConfig(scenario=s, persistence=p, resume=r, key=k, ids=i, after_interrupted=a, kill=kp)
        if not_applicable_reason(cfg) is not None:
            continue
        cid = f'X-{s}-{p}-{r}-{k}-{i}-{a}-{kp}'.replace('+', '_')
        cells.append(CuratedCell(id=cid, finding='X', claim='full product (unchecked)', expected='', config=cfg))
    return cells


def run_matrix(
    cells: Iterable[CuratedCell],
    *,
    jobs: int = 2,
    repeat: int = 1,
    timeout: float = 60.0,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Run cells (each `repeat` times) in a thread pool; each run spawns its own processes.

    Threads, not processes, orchestrate: the parent's work per cell is waiting on child
    processes and reading small SQLite files, so a thread pool gives real parallelism (the
    children) without pickling records. Results come back in the input order.
    """
    cell_list = list(cells)
    runs: dict[tuple[int, int], dict[str, Any]] = {}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {
            pool.submit(run_cell, c.id, c.config, timeout=timeout): (i, n)
            for i, c in enumerate(cell_list)
            for n in range(repeat)
        }
        for done, fut in enumerate(as_completed(futures), start=1):
            i, n = futures[fut]
            runs[(i, n)] = fut.result()
            if progress:
                r = runs[(i, n)]
                progress(
                    f'[{done}/{len(futures)}] {cell_list[i].id} (run {n + 1}/{repeat}): {r["verdict"]} '
                    f'{r["effect_counts"]} {r["duration_s"]}s'
                )
    records: list[dict[str, Any]] = []
    for i, c in enumerate(cell_list):
        first = runs[(i, 0)]
        reps = [runs[(i, n)] for n in range(repeat)]
        rec = dict(first)
        rec.update(
            id=c.id,
            finding=c.finding,
            finding_title=FINDINGS.get(c.finding, 'Full product'),
            claim=c.claim,
            expected=c.expected or None,
            expectation=(None if not c.expected else ('met' if first['verdict'] == c.expected else 'differs')),
            repeats=[
                {
                    'verdict': r['verdict'],
                    'effect_counts': r['effect_counts'],
                    'fingerprint': r['fingerprint'],
                    'duration_s': r['duration_s'],
                }
                for r in reps
            ],
            reproducible=len(
                {(r['verdict'], tuple(sorted(r['effect_counts'].items())), r['fingerprint']) for r in reps}
            )
            == 1,
        )
        records.append(rec)
    if progress:
        progress(f'{len(cell_list)} cells x {repeat} run(s) in {time.monotonic() - started:.1f}s')
    return records
