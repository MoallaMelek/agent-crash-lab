# Independent verification

A second pass over Agent Crash Lab by a reviewer who did not write it, on 2026-11-22. The goal
was to find wrong assumptions, misleading claims and bugs. Target: `pydantic-ai-slim 2.71.0` and
`pydantic-ai-harness 0.71.0` (pinned). Source reference: pydantic-ai commit `463a6d5`.

## Summary

- The lab does what it says. Every kill is a real `SIGKILL` (`returncode == -9`), and nothing is
  logged after the kill marker. Every verdict that was traced back by hand follows from the raw
  FakePay rows, the StepPersistence tables and the history file.
- Verdicts and effect counts are deterministic: two fresh `--repeat 2` runs and the recorded
  `--repeat 3` evidence agree on all 92 cells, with identical fingerprints.
- Two oracle bugs were found and fixed. Both affected the generated **responsibility** labels, not
  verdicts. Several wording overclaims were corrected. No expectation was changed.

## What was checked, and how

| Area | Method | Result |
|---|---|---|
| Clean install | `git clone` to a temp dir, then `docs/USAGE.md` literally: `uv sync`, `crashlab doctor`, `list`, `run` (with `--set` and `custom`), `matrix --quick`, `report`, and the coverage command. | All worked. `doctor` reports attempt 1 `rc=-9`. "About a minute on 2 cores" holds (about 55 s per repeat on 2 cores). |
| Static checks | `ruff check`, `ruff format --check`, `mypy` (strict, `src/`). | Clean. |
| Tests | Full `pytest`, Python 3.13. Also Python 3.11 (committed tree) with `matrix --quick --check`. | 105 passed (about 42 s) before fixes, 107 after. Unit subset: under 1 s. Coverage 92 %. Python 3.11: 105 passed, quick matrix 15/15. |
| Pydantic APIs | Read the installed 2.71.0 / 0.71.0 source for every imported symbol. Diffed the cited files against `463a6d5`. | All symbols exist with the used signatures. Cited files are identical, except two `# noqa` comments, as the docs say. |
| Line references | Checked 20 `ARCHITECTURE.md` and docstring references against `463a6d5`. | All point at the code described. |
| Kill points | Read `faults.py` and `child.py` against `_agent_graph.py`, `capabilities/combined.py` and `step_persistence/_capability.py`. Ran a step cell in-process to log hook order. | Order confirmed: ledger `started` → tool → ledger `completed` → `ToolSettledSignal` → probe `after_node_run` → snapshot save. |
| Cells by hand | `crashlab run --keep-workdir` for H1–H4, F1 (iter_naive next-request), F2 (fresh and stable, default and frontier), F4 (reissue, repair-trust, both exception cells), `F1-step-during-snapshot-write`, two parallel-batch cells, `S-kill-not-reached` and the MISSING cell. Queried `fakepay.db`, `steps.db` (`runs`, `snapshots`, `tool_effects`, `events`), `history.json` and `timeline.jsonl` directly with Python `sqlite3`. | Each verdict matches the raw records. |
| Determinism | `matrix -j 4 --repeat 2 --check` twice, compared with `evidence/2026-11-22/results.json`. | 0 verdict or count differences, 0 fingerprint differences, 92/92 reproducible each time. |
| Viewer | Built with `crashlab report`. Checked for URLs, compared the embedded JSON with `results.json`, and rendered it in headless Chrome. | No URLs and no network requests. The embedded data equals `results.json`. The banner says "Recorded evidence … does not execute anything". No JS errors. |
| Hygiene | Grepped tracked files and evidence for credentials and for `/home`, `/root` and `/tmp` paths. | No secrets. Evidence contains no workdir paths, because `--keep-workdir` is off in matrix runs. |

## API facts confirmed in the installed source

- **ALLOW_MODEL_REQUESTS.** `check_allow_model_requests()` is called by the provider models, but
  not by `FunctionModel` or `TestModel`. The guard therefore blocks real models without affecting
  the scripted one.
- **Sync model functions.** A sync `FunctionModel` function runs via `run_in_executor`
  (`models/function.py:178-181`). A `SIGKILL` from that worker thread kills the whole process.
- **Hook order.** `CombinedCapability` dispatches `after_node_run` and `after_tool_execute` in
  reverse (`combined.py:664`, `:910`). Core adds two `outermost` capabilities (`ToolSearch`,
  `PendingMessageDrainCapability`), so `sort_capabilities` runs. The runtime dispatch list keeps
  the lab's relative order: `ToolSettledSignal`, `StepPersistence`, `NodeBoundaryProbe`. The
  hook order was also confirmed empirically.
- **The wrapped store method.** StepPersistence writes through `self.store.save_snapshot` and
  `self.store.record_tool_effect`. `for_run` uses `dataclasses.replace`, which keeps the
  instrumented store. `SqliteStepStore.save_snapshot` commits in one `BEGIN IMMEDIATE`
  transaction.
- **`continue_run(include_interrupted=)`.** It calls `latest_snapshot(...)` and raises
  `LookupError` when that returns `None`. `inspect_recovery` reads facts only.
- **`DeferredToolResults(calls=, approvals=)`.** `_handle_deferred_tool_results` builds a
  `CallToolsNode` with the given results. `_tool_execution.py:548-560` requires a result for
  every eligible call once any result is given. This use for never-deferred calls is undocumented,
  and the lab labels it `framework_observed`.
- **`repair_messages`.** It is public in `pydantic_ai.messages`. It synthesizes `interrupted`
  returns with `pydantic_ai_synthesized_tool_return` metadata.
- **`iter_folded`.** The tool returns sit in the yielded `ModelRequestNode.request` until that
  request runs (`exp3_output.txt`). StepPersistence folds `result.request` the same way in
  `after_node_run`.
- **Response origin.** `ModelResponse.run_id` and `timestamp` survive a JSON round trip, and
  core does not re-stamp `run_id` on `message_history`. `originating_response` takes the newest
  response that carries the id, so a re-asked model yields a new key, as claimed.
- **Fresh vs stable ids.** Stable ids are a function of the history: `call-charge-<n>`, so a
  restart repeats `call-charge-1`. Fresh ids are `call-a<attempt>-n<invocation>-<part>`.
- **Run id reuse.** Core rejects a `run_id` that already appears in `message_history`
  (`_agent_graph.py:465`, exp6).

## Issues found and fixed

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | Medium | **Responsibility mislabel.** When `continue_default` returned a prompt-only frontier snapshot, the model was re-asked. With `ids=stable` it emitted `call-charge-1` again, and the oracle attributed the second execution to "resuming a history … executes the dangling call" (`framework_documented`). This hid the stable-id test artefact in `F2-step-frontier-default-tcid-stable`, which is OK only because of stable ids. It also mislabelled `F2-step-frontier-default-origin-stable`. | `oracle.build_responsibility` now treats a call as re-asked when attempt 2's model emitted it in a new response (from `model_responded` events), not only after a restart. Both cells now carry `scripted_model_behaviour: … only because ids=stable`. Regression test added. |
| 2 | Low | Reconcile cells that injected a provider result (H4, F1/F3/F5 reconcile) were also labelled "Calls already answered in the persisted history are not re-run". They were not answered in the history; reconciliation answered them. | That factor is suppressed when reconciliation injected a result. Regression test added. |
| 3 | Low | Overclaims in wording: <br>- `MISSING` was described as "the run reports success"; the run actually ends with "STATUS UNKNOWN". <br>- "`run_id` keys break on *every* resume"; reconcile recovers them (`F3-iter-naive-reconcile-runid`). <br>- `ARCHITECTURE.md`: "Nothing about this tool cycle has been persisted by anyone" at `after_tool_node`; the StepPersistence ledger has written `completed`. <br>- The H4 claim "without any key" leaves out that the lookup uses the `tool_call_id` stored as provider metadata. <br>- Three claims used "safe". | Reworded in `ENGINEERING_DECISIONS.md`, `ARCHITECTURE.md`, `keys.py`, and the claims and `FINDINGS` in `matrix.py`. |
| 4 | Low | `UPSTREAM_NOTES.md` called `F2-step-frontier-default-tcid-fresh` "continue_run with its defaults". That cell uses `capture_frontier=True`, and its LookupError counterpart is an application fallback. The note also did not say that "fresh ids" are scripted, and said folding "fixes" the window although `F1-iter-folded-after-tool-node` still duplicates. | Clarified all three points. |
| 5 | Trivial | `ENGINEERING_DECISIONS.md` gave the fresh-id format as `call-<attempt>-<n>`. `PYDANTIC_RESEARCH.md` pointed to `experiments/` instead of `research-experiments/`, and listed parallel-batch and ledger-to-snapshot kills as untested although the lab now covers them. | Corrected. |

No expected verdict was changed. Fixes 1–3 change the text of `responsibility`, the claims and the
finding titles in the records, so the evidence was regenerated:
`matrix -j 4 --repeat 3 --check --out evidence/2026-11-22`, then copied to
`docs/evidence/latest/`. Verdicts, counts and fingerprints are identical to the evidence before
the fixes. Expectations met: 92/92. Reproducible: 92/92.

## Residual risks and limits (not fixed; by design or out of scope)

- **SIGKILL is not power loss.** The caveat is present in `ENGINEERING_DECISIONS.md`,
  `ARCHITECTURE.md`, the FakePay docstring and the viewer footer.
- **`during_snapshot_write` kills before the delegate call**, not inside the SQLite transaction.
  Treating the two as equivalent is labelled INFERRED.
- **The model is scripted.** Fresh ids model provider behaviour, and `reissue` models a plausible
  LLM reaction. Neither was observed from a real provider or LLM.
- **Stable ids are the default axis value.** Every OK cell where the model was re-asked with
  stable ids now carries the "only because ids=stable" factor. Readers of the summary table alone
  will not see it; the README should say so.
- **The probes run in every cell.** `ToolSettledSignal` and `NodeBoundaryProbe` are extra
  capabilities, plus fsync'd timeline writes. They return their inputs unchanged. Hook order with
  and without them was not compared beyond reading the dispatch code.
- **Reconcile matches by `call_ref` metadata when no key exists.** With stable ids reused across
  orders or runs this would match the wrong charge. Only one order is tested.
- **Evidence provenance.** At review time the evidence recorded a dirty tree, because the fixes
  above were not yet committed. The published evidence was regenerated afterwards from a clean
  commit (see the banner in `docs/evidence/latest/summary.md`), with identical verdicts and counts.
