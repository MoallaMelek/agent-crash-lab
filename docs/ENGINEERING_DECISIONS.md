# Engineering decisions

This records what the project became after the research in
[`PYDANTIC_RESEARCH.md`](PYDANTIC_RESEARCH.md), and the alternatives that were rejected.
Target versions: `pydantic-ai-slim 2.71.0` and `pydantic-ai-harness 0.71.0`, the latest on PyPI on 2026-11-22.

## 1. What the research changed

The original brief was a fault-injection toolkit that shows whether recovery after a crash
duplicates side effects. The research found that most of that is already covered:

- Pydantic AI already has a crash-recovery layer. The harness `StepPersistence` capability keeps a
  tool-effect ledger (`started` / `completed` / `failed`), `interrupted` snapshots,
  `inspect_recovery()` and `annotate_tool_effect(idempotency_key=...)`.
- The docs for every durable engine already say that the tool in flight runs at least once, and
  that side-effecting tools must be made idempotent.
- Upstream already has a hard-kill subprocess test (`test_frontier.py`).
- The core duplicate after a crash is already reported, with a repro, in issue **#10926**.

So a lab whose headline is *"naive retry duplicates, idempotency keys fix it"* would repeat
the documentation. It would teach a Pydantic engineer nothing.

The experiments did surface facts that are **not** in the docs. Each one depends on *which*
resume path and *which* key an application uses:

1. Saving `run.all_messages()` at every `agent.iter` node leaves a crash window that lasts a
   whole model request. The tool return sits in `node.request` until that request finishes.
2. An idempotency key built on `tool_call_id` only works on resume paths that keep the persisted
   `ModelResponse`. Two paths ask the model again, and a real provider then mints a new id:
   - `StepPersistence`'s default `continue_run`, which reads `complete` snapshots only, after a
     kill in the first tool cycle;
   - a restart, when no snapshot exists.
3. A key built on `ctx.run_id` breaks on *every* resume, because the run id is new and core
   refuses to reuse it (#11951 is the same class of bug, inside the harness Shell).
4. After a synthesized `interrupted` return, a model may re-issue the call under a new id. No
   framework layer can see that.
5. `DeferredToolResults(calls={id: value})` resolves a dangling call that was never deferred.
   That makes it a working, though undocumented, reconciliation hook.

## 2. Decision

**Agent Crash Lab becomes an external, reproducible crash *matrix* plus a reference
reconciliation helper.** It is not an upstream test suite and not a framework patch.

The question it answers with measurements:

> For a side-effecting tool, which combination of **persistence pattern × resume mechanism ×
> effect-key strategy** gives exactly one external effect after a real `SIGKILL` at each
> execution boundary, and which combinations silently duplicate, lose or strand the effect?

### Axes

| Axis | Values |
|---|---|
| Kill point | none · before effect · after effect, before tool return · after tool node, before save · during next model request · mid parallel batch · during snapshot write |
| Persistence | `final_only` (save at end) · `iter_naive` (`all_messages()` per node) · `iter_folded` (+ `node.request`) · `step` (StepPersistence + `SqliteStepStore`, defaults) · `step_frontier` (`capture_frontier=True`) |
| Resume | `restart` (same prompt, fresh run) · `history` (`message_history`, no prompt) · `continue_default` / `continue_interrupted` (harness `continue_run`) · `repair` (`repair_messages` + prompt) · `reconcile` (ledger/dangling calls → provider lookup → `DeferredToolResults(calls=...)`) |
| Effect key | none · `run_id:tool_call_id` · `tool_call_id` · response origin (`ModelResponse.run_id`/timestamp + `tool_call_id`, the #11960 approach) · business key derived from arguments (`order_id`) |
| Model behaviour | stable vs fresh `tool_call_id`s · after an `interrupted` return: trusts it vs re-issues the call |

The full product of these axes is large, and most of it is uninformative. The lab runs a
**curated matrix**, where every cell tests one claim, and can also run the full product on demand.

### Ground truth (the oracle)

The simulated external system is a separate SQLite database, `FakePay`. It is the only source
of truth for "how many charges happened". Expected state: exactly one charge per order, and an
agent run that reaches its final output. Each cell is classified:

- `OK`: one charge, run completed.
- `DUPLICATE`: more than one charge.
- `MISSING`: zero charges, but the run reports success.
- `STRANDED`: the run could not complete; the recovery path raised or gave up.
- `AMBIGUOUS_RESOLVED`: one charge, but the agent's transcript disagrees with the provider.
  For example, the call was closed as `interrupted` while the charge exists.

Persisted-state consistency is checked separately. For example: the `StepPersistence` ledger
says `started` while the provider has the charge.

## 3. Constraints adopted

- **A real process death, not an exception.** The child process kills itself with
  `os.kill(os.getpid(), SIGKILL)` at an exact boundary, so no `finally`, `atexit` or flush runs.
  In-process exceptions are measured separately, and labelled as such. Pydantic handles them
  differently: it writes `ModelRequest(state='interrupted')`.
- **SIGKILL is not power loss.** The OS page cache survives a SIGKILL, so these results say
  nothing about `fsync` or torn writes after a power failure. The docs say so.
- **Deterministic.** `FunctionModel` scripts have no network and no LLM. "Fresh" ids are
  deterministic per model invocation (`call-<attempt>-<n>`), so they differ across processes
  but are reproducible. Each cell runs several times, and its outcome must be identical every time.
- **POSIX only** (Linux, macOS), because the lab relies on `SIGKILL`. Windows is out of scope.
- **Pinned** `pydantic-ai-slim==2.71.0`, `pydantic-ai-harness==0.71.0`, with `uv.lock`.
- **No claim of exactly-once.** The best cells show "exactly one effect *in this scenario*,
  given a provider that honours idempotency keys or supports lookup". That is end-to-end
  effectively-once under stated assumptions, not a framework guarantee.

## 4. Rejected alternatives

| Alternative | Why it was rejected |
|---|---|
| A "naive retry vs idempotency" demo | Already documented for every engine, and in #10926. Redundant. |
| An upstream PR adding a SIGKILL matrix to `tests/` | `tests/AGENTS.md` rejects process-spawning tests unless a claim depends on the process boundary. `CONTRIBUTING.md` requires a maintainer-approved issue first. Out of scope without separate approval. |
| Patching Pydantic AI (for example, auto-reconcile on resume) | No bug was established. The behaviour matches the docs. A code change needs maintainer agreement (policy). |
| Testing Temporal, DBOS or Prefect for real | Needs servers and more set-up than a reviewer can reproduce in a minute. Their docs already state at-least-once delivery. Left as future work. |
| Logfire/OTel spans as evidence | Spans buffered in memory are lost on SIGKILL. An append-only event log written before each boundary is more reliable for crash evidence. |
| A live hosted simulator | Running untrusted subprocess kills on a public server is not justified. A fake simulator in the browser would misrepresent the work. The public demo is a **recorded-evidence viewer** only. |

## 5. What would be worth showing upstream (not done without approval)

`docs/UPSTREAM_NOTES.md` drafts a short comment for #10926, plus a possible docs section,
"Crash windows and idempotency keys". Nothing is posted. `CONTRIBUTING.md` asks for a
human-written explanation, so the owner should rewrite it in his own words.
