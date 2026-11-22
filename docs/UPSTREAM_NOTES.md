# DRAFT: not posted. The owner must rewrite this in his own words before using it.

Draft comment for pydantic/pydantic-ai#10926. Every claim cites a cell from
`docs/evidence/latest/results.json`: pydantic-ai-slim 2.71.0, pydantic-ai-harness 0.71.0,
real SIGKILL, each cell run 3 times with identical outcomes.

---

I measured this on 2.71.0 with a SIGKILL matrix. The issue reproduces: `H2-naive-history-duplicates`.
Three resume-path details seem to matter for the idempotency-key advice.

1. If you save `run.all_messages()` after every `agent.iter` node, a kill during the next model
   request still re-runs the tool, because the returns are only in `node.request`
   (`F1-iter-naive-next-request`). Folding `node.request` into the saved history fixes this
   (`F1-iter-folded-next-request`).
2. A `ctx.tool_call_id` key only holds if the resume keeps the persisted `ModelResponse`. Two paths
   re-ask the model, and with provider-style fresh ids they charge twice:
   - StepPersistence `continue_run` with its defaults (`F2-step-frontier-default-tcid-fresh`);
   - the restart after `LookupError` (`F2-step-default-tcid-fresh`).

   `include_interrupted=True` passes (`F2-step-frontier-interrupted-tcid-fresh`).
3. A `ctx.run_id`-based key broke on every re-executing resume I tried
   (`F3-iter-naive-history-runid`, `F3-step-frontier-interrupted-runid`). A key built from the response's
   `run_id` and timestamp, as in #11960, survives (`F3-iter-naive-history-origin`).

Separately, after a synthesized `interrupted` return, a model that retries charges again under a new
id (`F4-repair-reissue`). Only a business-level key catches that (`F4-repair-reissue-business`).

`DeferredToolResults(calls={id: ...})` plus `approvals={id: True}` works as a reconciliation hook
for dangling calls that were never deferred (`F5-parallel-iter-naive-reconcile`).

Would a short docs section on this be welcome?
