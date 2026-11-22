# Agent Crash Lab: matrix summary

Recorded 2026-11-22T21:17:06+00:00 with pydantic-ai-slim 2.71.0, pydantic-ai-harness 0.71.0, Python 3.13.18 on Linux x86_64; lab commit 00aa8553948c.

92 cells, each run 3 time(s). Verdicts: AMBIGUOUS_RESOLVED 3, DUPLICATE 35, KILL_NOT_REACHED 1, MISSING 1, NOT_APPLICABLE 4, OK 48. Expectations: met 92. Reproducible: 92/92.

Verdicts count FakePay effects for one order (expected: exactly one charge, plus one notification
where the scenario sends one) and whether the run completed. "Exactly one" here means: in this
scenario, with a provider that honours idempotency keys or supports lookup. Not exactly-once.

## H: Headline demo

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `H1-no-crash` | iter_naive | history | none | stable | none | OK | OK | charge 1 | yes |
| `H2-naive-history-duplicates` | iter_naive | history | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `H3-business-key-dedupes` | iter_naive | history | business | stable | after_effect | OK | OK | charge 1 | yes |
| `H4-reconcile-dedupes` | iter_naive | reconcile | none | stable | after_effect | OK | OK | charge 1 | yes |

## B: Baselines without a crash

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `B-final_only-single_charge` | final_only | history | none | stable | none | OK | OK | charge 1 | yes |
| `B-iter_naive-single_charge` | iter_naive | history | none | stable | none | OK | OK | charge 1 | yes |
| `B-iter_folded-single_charge` | iter_folded | history | none | stable | none | OK | OK | charge 1 | yes |
| `B-step-single_charge` | step | continue_default | none | stable | none | OK | OK | charge 1 | yes |
| `B-step_frontier-single_charge` | step_frontier | continue_default | none | stable | none | OK | OK | charge 1 | yes |
| `B-iter_naive-charge_then_notify` | iter_naive | history | none | stable | none | OK | OK | charge 1, notify 1 | yes |
| `B-step-charge_then_notify` | step | continue_default | none | stable | none | OK | OK | charge 1, notify 1 | yes |
| `B-iter_naive-parallel_batch` | iter_naive | history | none | stable | none | OK | OK | charge 1, notify 1 | yes |
| `B-step_frontier-parallel_batch` | step_frontier | continue_default | none | stable | none | OK | OK | charge 1, notify 1 | yes |

## F1: Per-node all_messages() persistence loses tool returns during the next model request

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `F1-iter-naive-next-request` | iter_naive | history | none | stable | during_next_model_request | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F1-iter-folded-next-request` | iter_folded | history | none | stable | during_next_model_request | OK | OK | charge 1 | yes |
| `F1-step-next-request` | step | continue_default | none | stable | during_next_model_request | OK | OK | charge 1 | yes |
| `F1-step-frontier-next-request` | step_frontier | continue_default | none | stable | during_next_model_request | OK | OK | charge 1 | yes |
| `F1-final-only-next-request` | final_only | history | none | stable | during_next_model_request | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F1-iter-naive-after-tool-node` | iter_naive | history | none | stable | after_tool_node | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F1-iter-folded-after-tool-node` | iter_folded | history | none | stable | after_tool_node | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F1-iter-naive-next-request-business` | iter_naive | history | business | stable | during_next_model_request | OK | OK | charge 1 | yes |
| `F1-iter-naive-next-request-reconcile` | iter_naive | reconcile | none | stable | during_next_model_request | OK | OK | charge 1 | yes |
| `F1-notify-iter-naive-next-request` | iter_naive | history | none | stable | during_next_model_request | DUPLICATE | DUPLICATE | charge 2, notify 1 | yes |
| `F1-notify-iter-folded-next-request` | iter_folded | history | none | stable | during_next_model_request | OK | OK | charge 1, notify 1 | yes |
| `F1-step-after-tool-node` | step | continue_default | none | stable | after_tool_node | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F1-step-during-snapshot-write` | step | continue_default | none | stable | during_snapshot_write | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F1-step-after-snapshot-write` | step | continue_default | none | stable | after_snapshot_write | OK | OK | charge 1 | yes |
| `F1-step-frontier-during-snapshot-write-interrupted` | step_frontier | continue_interrupted | none | stable | during_snapshot_write | DUPLICATE | DUPLICATE | charge 2 | yes |

## F2: tool_call_id keys break on resume paths that re-ask the model

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `F2-iter-naive-history-tcid-fresh` | iter_naive | history | tool_call_id | fresh | after_effect | OK | OK | charge 1 | yes |
| `F2-step-default-tcid-fresh` | step | continue_default | tool_call_id | fresh | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F2-step-default-tcid-stable` | step | continue_default | tool_call_id | stable | after_effect | OK | OK | charge 1 | yes |
| `F2-step-frontier-default-tcid-fresh` | step_frontier | continue_default | tool_call_id | fresh | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F2-step-frontier-default-tcid-stable` | step_frontier | continue_default | tool_call_id | stable | after_effect | OK | OK | charge 1 | yes |
| `F2-step-frontier-interrupted-tcid-fresh` | step_frontier | continue_interrupted | tool_call_id | fresh | after_effect | OK | OK | charge 1 | yes |
| `F2-restart-tcid-fresh` | iter_naive | restart | tool_call_id | fresh | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F2-final-only-tcid-fresh` | final_only | history | tool_call_id | fresh | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F2-step-frontier-default-origin-stable` | step_frontier | continue_default | response_origin | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F2-step-frontier-interrupted-origin-fresh` | step_frontier | continue_interrupted | response_origin | fresh | after_effect | OK | OK | charge 1 | yes |
| `F2-step-default-business-fresh` | step | continue_default | business | fresh | after_effect | OK | OK | charge 1 | yes |
| `F2-step-frontier-default-business-fresh` | step_frontier | continue_default | business | fresh | after_effect | OK | OK | charge 1 | yes |
| `F2-final-only-business-fresh` | final_only | restart | business | fresh | after_effect | OK | OK | charge 1 | yes |
| `F2-parallel-step-default-tcid-fresh` | step | continue_default | tool_call_id | fresh | mid_parallel_batch | DUPLICATE | DUPLICATE | charge 2, notify 1 | yes |

## F3: run_id-based keys break on every resume that re-executes the call

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `F3-iter-naive-history-runid` | iter_naive | history | run_id+tool_call_id | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F3-iter-folded-history-runid` | iter_folded | history | run_id+tool_call_id | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F3-iter-naive-next-request-runid` | iter_naive | history | run_id+tool_call_id | stable | during_next_model_request | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F3-step-frontier-interrupted-runid` | step_frontier | continue_interrupted | run_id+tool_call_id | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F3-iter-naive-history-tcid` | iter_naive | history | tool_call_id | stable | after_effect | OK | OK | charge 1 | yes |
| `F3-iter-naive-history-origin` | iter_naive | history | response_origin | stable | after_effect | OK | OK | charge 1 | yes |
| `F3-step-frontier-interrupted-origin` | step_frontier | continue_interrupted | response_origin | stable | after_effect | OK | OK | charge 1 | yes |
| `F3-iter-naive-reconcile-runid` | iter_naive | reconcile | run_id+tool_call_id | stable | after_effect | OK | OK | charge 1 | yes |

## F4: A model may re-issue a call after a synthesized interrupted return

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `F4-repair-trust` | iter_naive | repair | none | stable | after_effect | AMBIGUOUS_RESOLVED | AMBIGUOUS_RESOLVED | charge 1 | yes |
| `F4-repair-reissue` | iter_naive | repair | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F4-repair-reissue-tcid` | iter_naive | repair | tool_call_id | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F4-repair-reissue-origin` | iter_naive | repair | response_origin | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F4-repair-reissue-business` | iter_naive | repair | business | stable | after_effect | OK | OK | charge 1 | yes |
| `F4-repair-before-effect-trust` | iter_naive | repair | none | stable | before_effect | MISSING | MISSING | charge 0 | yes |
| `F4-repair-before-effect-reissue` | iter_naive | repair | none | stable | before_effect | OK | OK | charge 1 | yes |
| `F4-step-frontier-repair-reissue` | step_frontier | repair | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F4-exception-final-only-trust` | final_only | history | none | stable | after_effect (exception) | AMBIGUOUS_RESOLVED | AMBIGUOUS_RESOLVED | charge 1 | yes |
| `F4-exception-final-only-reissue` | final_only | history | none | stable | after_effect (exception) | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F4-exception-final-only-reissue-business` | final_only | history | business | stable | after_effect (exception) | OK | OK | charge 1 | yes |
| `F4-exception-step-interrupted-trust` | step | continue_interrupted | none | stable | after_effect (exception) | AMBIGUOUS_RESOLVED | AMBIGUOUS_RESOLVED | charge 1 | yes |
| `F4-exception-step-interrupted-reissue` | step | continue_interrupted | none | stable | after_effect (exception) | DUPLICATE | DUPLICATE | charge 2 | yes |

## F5: DeferredToolResults(calls=...) works as a reconciliation hook for never-deferred calls

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `F5-iter-naive-reconcile-before-effect` | iter_naive | reconcile | none | stable | before_effect | OK | OK | charge 1 | yes |
| `F5-iter-folded-reconcile-after-effect` | iter_folded | reconcile | none | stable | after_effect | OK | OK | charge 1 | yes |
| `F5-step-frontier-reconcile-after-effect` | step_frontier | reconcile | none | stable | after_effect | OK | OK | charge 1 | yes |
| `F5-step-frontier-reconcile-fresh` | step_frontier | reconcile | none | fresh | after_effect | OK | OK | charge 1 | yes |
| `F5-step-default-reconcile-degraded` | step | reconcile | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F5-step-default-reconcile-degraded-business` | step | reconcile | business | stable | after_effect | OK | OK | charge 1 | yes |
| `F5-final-only-reconcile-degraded` | final_only | reconcile | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `F5-notify-reconcile-after-charge` | iter_naive | reconcile | none | stable | after_effect | OK | OK | charge 1, notify 1 | yes |
| `F5-notify-history-after-notify` | iter_naive | history | none | stable | after_effect @notify_customer | DUPLICATE | DUPLICATE | charge 1, notify 2 | yes |
| `F5-notify-reconcile-after-notify` | iter_naive | reconcile | none | stable | after_effect @notify_customer | OK | OK | charge 1, notify 1 | yes |
| `F5-parallel-iter-naive-history` | iter_naive | history | none | stable | mid_parallel_batch | DUPLICATE | DUPLICATE | charge 2, notify 1 | yes |
| `F5-parallel-iter-naive-reconcile` | iter_naive | reconcile | none | stable | mid_parallel_batch | OK | OK | charge 1, notify 1 | yes |
| `F5-parallel-iter-naive-business` | iter_naive | history | business | stable | mid_parallel_batch | OK | OK | charge 1, notify 1 | yes |
| `F5-parallel-step-frontier-interrupted` | step_frontier | continue_interrupted | none | stable | mid_parallel_batch | DUPLICATE | DUPLICATE | charge 2, notify 1 | yes |
| `F5-parallel-step-frontier-reconcile` | step_frontier | reconcile | none | stable | mid_parallel_batch | OK | OK | charge 1, notify 1 | yes |
| `F5-parallel-step-default` | step | continue_default | none | stable | mid_parallel_batch | DUPLICATE | DUPLICATE | charge 2, notify 1 | yes |

## R: Restart without persisted state

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `R-iter-naive-restart-after-effect` | iter_naive | restart | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `R-iter-naive-restart-before-effect` | iter_naive | restart | none | stable | before_effect | OK | OK | charge 1 | yes |
| `R-step-restart-after-snapshot` | step | restart | none | stable | after_snapshot_write | DUPLICATE | DUPLICATE | charge 2 | yes |

## D: Documented framework behaviour and honest N/A handling

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `D-iter-naive-history-before-effect` | iter_naive | history | none | stable | before_effect | OK | OK | charge 1 | yes |
| `D-step-frontier-interrupted-before-effect` | step_frontier | continue_interrupted | none | stable | before_effect | OK | OK | charge 1 | yes |
| `D-step-frontier-interrupted-after-effect` | step_frontier | continue_interrupted | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `D-step-continue-default-after-effect` | step | continue_default | none | stable | after_effect | DUPLICATE | DUPLICATE | charge 2 | yes |
| `D-folded-history-next-request-answered` | iter_folded | history | none | stable | during_next_model_request | OK | OK | charge 1 | yes |
| `D-na-continue-without-store` | iter_naive | continue_default | none | stable | after_effect | NOT_APPLICABLE | NOT_APPLICABLE | charge 0 | yes |
| `D-na-history-with-store` | step | history | none | stable | after_effect | NOT_APPLICABLE | NOT_APPLICABLE | charge 0 | yes |
| `D-na-snapshot-kill-without-store` | iter_folded | history | none | stable | during_snapshot_write | NOT_APPLICABLE | NOT_APPLICABLE | charge 0 | yes |
| `D-na-parallel-kill-single-tool` | iter_naive | history | none | stable | mid_parallel_batch | NOT_APPLICABLE | NOT_APPLICABLE | charge 0 | yes |

## S: Lab self-checks

| cell | persistence | resume | key | ids | kill | expected | observed | effects | repro |
|---|---|---|---|---|---|---|---|---|---|
| `S-kill-not-reached` | iter_naive | history | none | stable | after_effect @notify_customer | KILL_NOT_REACHED | KILL_NOT_REACHED | charge 1 | yes |

