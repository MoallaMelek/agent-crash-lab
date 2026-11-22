# DRAFT: not posted. The owner must rewrite this in his own words before using it.

Draft comment for pydantic/pydantic-ai#10926. Every claim cites a cell from
`docs/evidence/latest/results.json`: pydantic-ai-slim 2.71.0, pydantic-ai-harness 0.71.0,
real SIGKILL, each cell run 3 times with identical outcomes.

---

I measured this on 2.71.0 with a SIGKILL matrix. The issue reproduces: `H2-naive-history-duplicates`.
Three resume-path details seem to matter for the idempotency-key advice. The "fresh ids" are a
scripted stand-in for a provider that mints a new id per response; no real provider was called.

1. If you save `run.all_messages()` after every `agent.iter` node, a kill during the next model
   request still re-runs the tool, because the returns are only in `node.request`
   (`F1-iter-naive-next-request`). Folding `node.request` into the saved history closes this
   window (`F1-iter-folded-next-request`), though not the shorter one before the save itself
   (`F1-iter-folded-after-tool-node`).
2. A `ctx.tool_call_id` key only holds if the resume keeps the persisted `ModelResponse`. Two paths
   re-ask the model, and with provider-style fresh ids they charge twice:
   - `continue_run` with its default `include_interrupted=False`, under `capture_frontier=True`: the
     newest complete snapshot is the prompt only (`F2-step-frontier-default-tcid-fresh`);
   - an application that restarts the task after `continue_run` raises `LookupError`
     (`F2-step-default-tcid-fresh`, StepPersistence defaults).

   `include_interrupted=True` passes (`F2-step-frontier-interrupted-tcid-fresh`).
3. A `ctx.run_id`-based key broke on every re-executing resume I tried
   (`F3-iter-naive-history-runid`, `F3-step-frontier-interrupted-runid`). A key built from the response's
   `run_id` and timestamp, as in #11960, survives (`F3-iter-naive-history-origin`).

Separately, after a synthesized `interrupted` return, a model that retries charges again under a new
id (`F4-repair-reissue`). Of the key strategies tested, only a business-level key catches that (`F4-repair-reissue-business`).

`DeferredToolResults(calls={id: ...})` plus `approvals={id: True}` works as a reconciliation hook
for dangling calls that were never deferred (`F5-parallel-iter-naive-reconcile`).

Would a short docs section on this be welcome?
