"""The oracle: classify a finished cell from facts, and explain the classification.

Ground truth is FakePay. The task should produce exactly one effect per expected tool (one
charge; one notification where the scenario sends one) *and* reach a final output. Verdicts,
checked in this order:

* `NOT_APPLICABLE`: the combination cannot be built honestly (see `config.not_applicable_reason`).
* `LAB_ERROR`: the lab itself misbehaved (unexpected exit code, missing kill marker, timeout in
  attempt 1). Never a finding about Pydantic AI.
* `KILL_NOT_REACHED`: a kill point was configured but attempt 1 completed without reaching it.
* `DUPLICATE`: more than one effect for some expected tool (checked first among real outcomes:
  an extra charge is an external fact even if the run then failed).
* `STRANDED`: the recovery attempt raised or timed out; the run could not complete.
* `MISSING`: zero effects for some expected tool, but the run completed.
* `AMBIGUOUS_RESOLVED`: exactly one of each, but the final transcript disagrees with the
  provider (a call closed as `interrupted`, or the agent never saw the effect's id).
* `OK`: exactly one of each, run completed, transcript consistent.

`explanation` and `responsibility` are generated from the recorded facts (timeline, provider
rows, resume plan, ledger), not written per cell.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from crashlab.config import CHARGE_TOOL, NOTIFY_TOOL, CellConfig

KILL_POINT_WHERE = {
    'before_effect': 'inside {tool}, before the FakePay call',
    'after_effect': 'inside {tool}, after FakePay committed and before the tool returned',
    'after_tool_node': 'after the CallToolsNode that ran {tool} finished, before any snapshot or history save',
    'during_next_model_request': 'inside the model request that follows the {tool} return',
    'mid_parallel_batch': 'after charge_order settled, while notify_customer (same response) was in flight',
    'during_snapshot_write': 'entering the StepPersistence snapshot save that carries the {tool} return (not committed)',
    'after_snapshot_write': 'right after the StepPersistence snapshot carrying the {tool} return committed',
}

DOC_RESUME_EXECUTES = (
    'Resuming a history whose last ModelResponse has unanswered tool calls, without a new prompt, '
    'executes them (`_agent_graph.py:1286-1385`; docs/harness/step-persistence.md:195).'
)
DOC_INTERRUPTED = (
    'A dangling call closed out with a synthesized `outcome=interrupted` return does not say whether '
    'the effect happened (docs/harness/step-persistence.md:195).'
)
DOC_ANSWERED_NOT_RERUN = (
    'Calls already answered in the persisted history are not re-run (`CallToolsNode._answered_calls`).'
)
DOC_ITER_WINDOW = (
    "`run.all_messages()` excludes the tool returns while they sit in the yielded ModelRequestNode's "
    '`node.request`; a crash before that model request finishes loses them (exp3).'
)
DOC_LEDGER_WINDOW = (
    'StepPersistence snapshots per settled batch; a kill between the ledger write and the snapshot leaves '
    'an effect the snapshots do not carry (docs/harness/step-persistence.md:724-729).'
)

EFFECT_ID_RE = re.compile(r'\b(?:ch|nt)_\d{4}\b')


@dataclass
class CellFacts:
    """Everything the oracle may look at. Built by the runner from the workdir."""

    cell: CellConfig
    na_reason: str | None = None
    lab_error: str | None = None
    attempt1_rc: int | None = None
    attempt2_rc: int | None = None
    kill_event: dict[str, Any] | None = None
    final_attempt: int | None = None
    completed: bool = False
    final_error: str | None = None
    final_output: str | None = None
    final_returns: list[dict[str, Any]] = field(default_factory=list)
    effects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    resume: dict[str, Any] | None = None


@dataclass
class Classification:
    verdict: str
    effect_counts: dict[str, int]
    checks: list[dict[str, str]]
    explanation: str
    responsibility: list[dict[str, str]]
    fingerprint: str


def _counts(facts: CellFacts) -> dict[str, int]:
    return {tool: len(facts.effects.get(tool, [])) for tool in facts.cell.expected_effects()}


def _events(facts: CellFacts, event: str, attempt: int | None = None) -> list[dict[str, Any]]:
    return [e for e in facts.timeline if e.get('event') == event and (attempt is None or e.get('attempt') == attempt)]


def _mentioned_effect_ids(facts: CellFacts) -> set[str]:
    ids: set[str] = set()
    for r in facts.final_returns:
        if r.get('outcome') == 'success':
            ids.update(EFFECT_ID_RE.findall(str(r.get('content', ''))))
    return ids


def consistency_checks(facts: CellFacts) -> list[dict[str, str]]:
    """Cross-checks between the provider, the timeline, the transcript and the ledger."""
    checks: list[dict[str, str]] = []
    if facts.na_reason or facts.lab_error:
        return checks
    for tool in (CHARGE_TOOL, NOTIFY_TOOL):
        rows = facts.effects.get(tool, [])
        created = [e for e in _events(facts, 'effect_committed') if e.get('tool') == tool and not e.get('replayed')]
        if not rows and not created:
            continue
        ok = len(created) == len(rows)
        checks.append(
            {
                'name': f'timeline_vs_provider[{tool}]',
                'status': 'pass' if ok else 'fail',
                'detail': f'{len(created)} effect_committed (new) event(s) in the timeline, {len(rows)} row(s) in FakePay',
            }
        )
    if facts.completed:
        mentioned = _mentioned_effect_ids(facts)
        provider_ids = {r['id'] for rows in facts.effects.values() for r in rows}
        orphans = sorted(provider_ids - mentioned)
        phantom = sorted(mentioned - provider_ids)
        checks.append(
            {
                'name': 'transcript_vs_provider',
                'status': 'pass' if not orphans and not phantom else 'fail',
                'detail': (
                    'every provider effect is referenced by a successful tool return in the final transcript'
                    if not orphans and not phantom
                    else f'effects the final transcript never mentions: {orphans or "none"}; '
                    f'mentioned but absent from FakePay: {phantom or "none"}'
                ),
            }
        )
        for tool in facts.cell.expected_effects():
            tool_returns = [r for r in facts.final_returns if r.get('tool') == tool]
            if tool_returns and tool_returns[-1].get('outcome') != 'success' and facts.effects.get(tool):
                checks.append(
                    {
                        'name': f'interrupted_but_effect_exists[{tool}]',
                        'status': 'fail',
                        'detail': (
                            f'the last {tool} return is outcome={tool_returns[-1].get("outcome")}'
                            f'{" (synthesized)" if tool_returns[-1].get("synthesized") else ""}, '
                            f'but FakePay has {len(facts.effects[tool])} effect(s)'
                        ),
                    }
                )
    if facts.cell.uses_step_persistence:
        # Compare attempt 1's ledger with what existed when attempt 1 died: its own effects and snapshots.
        call_refs = {r.get('call_ref') for rows in facts.effects.values() for r in rows if r.get('attempt') == 1}
        snap_return_ids = {
            cid for s in facts.snapshots if s.get('run_id') == 'crashlab-run-a1' for cid in s.get('return_ids', [])
        }
        for rec in facts.ledger:
            if rec.get('run_id') != 'crashlab-run-a1':
                continue
            cid, status = rec.get('tool_call_id'), rec.get('status')
            has_effect = cid in call_refs
            if status == 'started' and has_effect:
                checks.append(
                    {
                        'name': f'ledger_vs_provider[{cid}]',
                        'status': 'info',
                        'detail': f'ledger says started (unknown_after_crash); attempt 1 committed a FakePay effect for {cid}',
                    }
                )
            elif status == 'started':
                checks.append(
                    {
                        'name': f'ledger_vs_provider[{cid}]',
                        'status': 'info',
                        'detail': f'ledger says started (unknown_after_crash); attempt 1 committed no FakePay effect for {cid}',
                    }
                )
            elif status == 'failed' and has_effect:
                checks.append(
                    {
                        'name': f'ledger_vs_provider[{cid}]',
                        'status': 'info',
                        'detail': f'ledger says failed, but attempt 1 committed a FakePay effect for {cid} '
                        '(documented: a failed tool may have made external changes)',
                    }
                )
            elif status == 'completed' and not has_effect:
                checks.append(
                    {
                        'name': f'ledger_vs_provider[{cid}]',
                        'status': 'fail',
                        'detail': f'ledger says completed but attempt 1 committed no FakePay effect for {cid}',
                    }
                )
            elif status == 'completed' and cid not in snap_return_ids:
                checks.append(
                    {
                        'name': f'ledger_vs_snapshots[{cid}]',
                        'status': 'info',
                        'detail': f'ledger says completed, but no attempt-1 snapshot carries the return for {cid}',
                    }
                )
    return checks


def _verdict(facts: CellFacts, counts: dict[str, int], checks: list[dict[str, str]]) -> str:
    if facts.na_reason:
        return 'NOT_APPLICABLE'
    if facts.lab_error:
        return 'LAB_ERROR'
    if facts.cell.expects_crash and facts.kill_event is None:
        return 'KILL_NOT_REACHED'
    if any(n > 1 for n in counts.values()):
        return 'DUPLICATE'
    if not facts.completed:
        return 'STRANDED'
    if any(n == 0 for n in counts.values()):
        return 'MISSING'
    if any(c['status'] == 'fail' for c in checks if c['name'].startswith(('interrupted_but', 'transcript_vs'))):
        return 'AMBIGUOUS_RESOLVED'
    return 'OK'


# --------------------------------------------------------------------- explanation


def _fmt_effects(events: list[dict[str, Any]]) -> str:
    parts = []
    for e in events:
        tag = 'replayed' if e.get('replayed') else 'new'
        key = e.get('idempotency_key')
        parts.append(
            f'{e.get("tool")} {e.get("tool_call_id")} -> {e.get("effect_id")} ({tag}{", key=" + _short_key(key) if key else ", no key"})'
        )
    return '; '.join(parts)


def _short_key(key: str) -> str:
    return key if len(key) <= 48 else key[:22] + '…' + key[-20:]


def _persisted_at_crash(facts: CellFacts) -> str:
    cell = facts.cell
    if cell.uses_step_persistence:
        a1 = [s for s in facts.snapshots if s.get('run_id') == 'crashlab-run-a1']
        if not a1:
            snap = 'no snapshot'
        else:
            last = a1[-1]
            snap = f'{len(a1)} snapshot(s), newest {last.get("state")} ending {last.get("tail", [])[-1:]}'
        ledger = [
            f'{r.get("tool_call_id")}={r.get("status")}' for r in facts.ledger if r.get('run_id') == 'crashlab-run-a1'
        ]
        return f'StepPersistence held {snap}; ledger {", ".join(ledger) or "empty"}'
    saves = _events(facts, 'history_saved', 1) + _events(facts, 'history_saved_on_error', 1)
    saves.sort(key=lambda e: e.get('seq', 0))
    if not saves:
        return f'{cell.persistence} had written no history file'
    last = saves[-1]
    src = ' (from capture_run_messages)' if last.get('event') == 'history_saved_on_error' else ''
    return f'the history file{src} held {last.get("n_messages")} message(s) ending {last.get("tail", [])}'


def build_explanation(facts: CellFacts, verdict: str, counts: dict[str, int]) -> str:
    cell = facts.cell
    if verdict == 'NOT_APPLICABLE':
        return f'Not run: {facts.na_reason}'
    if verdict == 'LAB_ERROR':
        return f'The lab failed, not the system under test: {facts.lab_error}'
    sentences: list[str] = []
    a1_effects = _events(facts, 'effect_committed', 1)
    if not cell.expects_crash:
        sentences.append(
            f'No fault was injected. Attempt 1 ran to completion; effects: {_fmt_effects(a1_effects) or "none"}.'
        )
    elif facts.kill_event is None:
        sentences.append(
            f'A {cell.crash} at {cell.kill} (tool {cell.kill_tool}) was configured, but attempt 1 completed '
            'without reaching it, so nothing was tested.'
        )
    else:
        where = KILL_POINT_WHERE.get(cell.kill, cell.kill).format(tool=cell.kill_tool)
        how = 'died by SIGKILL' if cell.crash == 'sigkill' else 'raised an injected in-process exception'
        before = f' after committing {_fmt_effects(a1_effects)}' if a1_effects else ' before any provider effect'
        sentences.append(f'Attempt 1 {how} at {cell.kill} ({where}){before}.')
        sentences.append(f'At that moment {_persisted_at_crash(facts)}.')
    if facts.resume is not None:
        r = facts.resume
        desc = f'Attempt 2 used resume={r["mechanism"]}'
        if r.get('fallback_restart'):
            note = r['notes'][0] if r.get('notes') else 'no persisted history'
            desc += f'; {note}'
        elif r['mechanism'] == 'restart':
            desc += ': a fresh run with the original prompt, ignoring persisted state'
        else:
            desc += (
                f' from {r["history_source"]} ({r.get("history_len")} message(s) ending '
                f'{(r.get("history_tail") or [])[-1:]})'
            )
            if r.get('user_prompt'):
                desc += ' plus a new prompt'
        if r.get('lookups'):
            looks = ', '.join(
                f'{lk["tool_call_id"]} by {lk["lookup"]} -> {lk["found_effect_id"] or "not found"} ({lk["action"]})'
                for lk in r['lookups']
            )
            desc += f'; reconciliation looked up {looks}'
        if r.get('degraded'):
            desc += '; reconciliation degraded: ' + '; '.join(r.get('notes', [])[1:] or ['no history to inject into'])
        sentences.append(desc + '.')
        a2_effects = _events(facts, 'effect_committed', 2)
        reissued = [
            e
            for e in _events(facts, 'model_invoked', 2)
            if any('interrupted' in outcomes for outcomes in (e.get('sees_returns') or {}).values())
        ]
        if reissued:
            sentences.append('The model saw an interrupted tool return during attempt 2.')
        sentences.append(f'It executed: {_fmt_effects(a2_effects) or "no provider call"}.')
    counts_txt = ', '.join(f'{n} {tool}' for tool, n in counts.items())
    if facts.completed:
        tail = f'The run completed with output {facts.final_output!r}.'
    else:
        tail = f'The run did not complete: {facts.final_error or "no successful attempt"}.'
    sentences.append(f'FakePay recorded {counts_txt}. {tail} Verdict: {verdict}.')
    return ' '.join(sentences)


def build_responsibility(facts: CellFacts, verdict: str) -> list[dict[str, str]]:
    """Contributing factors, each labelled with who owns it."""
    cell = facts.cell
    out: list[dict[str, str]] = []

    def add(category: str, factor: str) -> None:
        if not any(x['factor'] == factor for x in out):
            out.append({'category': category, 'factor': factor})

    if verdict in ('NOT_APPLICABLE', 'LAB_ERROR', 'KILL_NOT_REACHED'):
        add('lab', 'No claim about Pydantic AI: ' + verdict.lower().replace('_', ' ') + '.')
        return out
    a1 = _events(facts, 'effect_committed', 1)
    a2 = _events(facts, 'effect_committed', 2)
    a1_ids = {(e['tool'], e['tool_call_id']) for e in a1}
    r = facts.resume or {}
    restarted = r.get('mechanism') == 'restart' or bool(r.get('fallback_restart'))
    for e in a2:
        # After a restart the model is asked again even if it happens to emit the same (stable) id.
        same_call = (e['tool'], e['tool_call_id']) in a1_ids and not restarted
        if same_call:
            add('framework_documented', DOC_RESUME_EXECUTES)
        elif any(x['tool'] == e['tool'] for x in a1):
            if any(
                'interrupted' in str(m.get('sees_returns'))
                for m in _events(facts, 'model_invoked', 2)
                if m.get('k', 0) >= 1
            ):
                add(
                    'scripted_model_behaviour',
                    'After an interrupted return the scripted model issued a new call for the same effect; '
                    'no framework layer can link it to the first call (exp5).',
                )
            else:
                add(
                    'application_choice',
                    f'The resume path ({r.get("mechanism")}{", fallback restart" if r.get("fallback_restart") else ""}) '
                    'asked the model again, so the effect was requested by a new ModelResponse.',
                )
                if cell.ids == 'fresh':
                    add(
                        'scripted_model_behaviour',
                        'The model mints a fresh tool_call_id per response, as real providers do.',
                    )
                elif (e['tool'], e['tool_call_id']) in a1_ids:
                    add(
                        'scripted_model_behaviour',
                        f'The re-asked model emitted the same tool_call_id ({e["tool_call_id"]}) only because '
                        'ids=stable; real providers usually mint a new one.',
                    )
        if e.get('replayed'):
            add(
                'provider_assumption',
                f"FakePay honoured idempotency key strategy '{cell.key}' and returned the original effect; "
                'a provider without key support would have created a second one.',
            )
    if verdict == 'DUPLICATE':
        if cell.key == 'none':
            add('application_choice', 'The tool sends no idempotency key, so the provider cannot detect the repeat.')
        else:
            k1 = {e['tool']: e.get('idempotency_key') for e in a1}
            for e in a2:
                if not e.get('replayed') and e['tool'] in k1 and k1[e['tool']] != e.get('idempotency_key'):
                    add(
                        'application_choice',
                        f"Key strategy '{cell.key}' derived a different key on resume "
                        f'({_short_key(str(k1[e["tool"]]))} -> {_short_key(str(e.get("idempotency_key")))}).',
                    )
    if cell.kill == 'during_next_model_request' and cell.persistence == 'iter_naive':
        add('application_choice', DOC_ITER_WINDOW)
    if (
        cell.kill in ('after_tool_node', 'during_snapshot_write', 'mid_parallel_batch')
        and verdict == 'DUPLICATE'
        and cell.uses_step_persistence
    ):
        add('framework_documented', DOC_LEDGER_WINDOW)
    lookups = r.get('lookups') or []
    if any(lk['action'] == 'inject_result' for lk in lookups):
        add(
            'provider_assumption',
            'Reconciliation relies on the provider answering a lookup by key or by call metadata correctly.',
        )
        add(
            'framework_observed',
            '`DeferredToolResults(calls={id: value})` closes a never-deferred dangling call without executing it '
            '(works on 2.71.0; not documented for this use).',
        )
    if any(lk['action'] == 'execute' for lk in lookups) and any(lk['action'] == 'inject_result' for lk in lookups):
        add(
            'framework_observed',
            '`DeferredToolResults(approvals={id: True})` executes a never-deferred dangling call; needed because '
            'core requires a result for every dangling call once any is supplied (`_tool_execution.py:557`).',
        )
    if r.get('degraded'):
        add(
            'application_choice',
            'Reconciliation had no persisted ModelResponse to inject into and fell back to restart.',
        )
    if any(ret.get('synthesized') for ret in facts.final_returns):
        add('framework_documented', DOC_INTERRUPTED)
    if verdict == 'MISSING' and cell.after_interrupted == 'trust':
        add('scripted_model_behaviour', 'The scripted model trusted the interrupted return and did not retry.')
    if not a2 and facts.resume is not None and verdict == 'OK':
        add('framework_documented', DOC_ANSWERED_NOT_RERUN)
    if verdict == 'OK' and not cell.expects_crash:
        add('framework_documented', 'Baseline: one run, one effect.')
    if verdict == 'STRANDED':
        add('application_choice', f'The recovery path raised: {facts.final_error}.')
    if not out:
        add('framework_documented', 'Behaviour matches the documented resume semantics.')
    return out


# ------------------------------------------------------------------- fingerprint

_FP_FIELDS = (
    'event',
    'attempt',
    'tool',
    'tool_call_id',
    'point',
    'crash',
    'replayed',
    'effect_id',
    'status',
    'state',
    'yielded_node',
    'node',
    'next',
    'n_messages',
    'folded_request',
    'emits',
    'k',
    'sees_returns',
    'fallback_restart',
    'degraded',
    'deferred_calls',
    'deferred_approvals',
    'history_len',
    'step_index',
    'output',
)


def fingerprint(verdict: str, counts: dict[str, int], timeline: list[dict[str, Any]]) -> str:
    """Hash of the outcome and the id-normalised, order-insensitive event multiset.

    Run ids, timestamps, pids and idempotency keys (which can embed run ids and response
    timestamps) are excluded; whether a key was present is kept. Event order is ignored because
    StepPersistence's store writes run on worker threads and may interleave differently.
    """
    norm = []
    for e in timeline:
        d = {k: e[k] for k in _FP_FIELDS if k in e}
        if 'idempotency_key' in e:
            d['has_key'] = e['idempotency_key'] is not None
        norm.append(json.dumps(d, sort_keys=True, default=str))
    payload = json.dumps({'verdict': verdict, 'counts': counts, 'events': sorted(norm)}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def classify(facts: CellFacts) -> Classification:
    counts = _counts(facts)
    checks = consistency_checks(facts)
    verdict = _verdict(facts, counts, checks)
    return Classification(
        verdict=verdict,
        effect_counts=counts,
        checks=checks,
        explanation=build_explanation(facts, verdict, counts),
        responsibility=build_responsibility(facts, verdict),
        fingerprint=fingerprint(verdict, counts, facts.timeline),
    )
