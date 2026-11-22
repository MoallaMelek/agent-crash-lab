# Pydantic AI: reliability, persistence and crash recovery of tool side effects

**The question this answers.** A Pydantic AI agent runs a tool that has an external side effect, for example charging a payment. The process dies before the run's execution state is durably recorded. Then it resumes. What happens?

Evidence labels used throughout:

- **VERIFIED**: I read it in the source, or ran it in `docs/research-experiments/` against the installed release.
- **DOCUMENTED**: the docs in the clone at `463a6d5` say so. I did not test it.
- **INFERRED**: my own reasoning. Not confirmed by code or by a maintainer.

Date of research: 2026-11-22.

---

## 0. Versions and sources

| Package | Latest on PyPI (checked with `pip index versions`) | Installed in a local venv |
|---|---|---|
| `pydantic-ai` | 2.71.0 | (I installed `-slim`) |
| `pydantic-ai-slim` | 2.71.0 | 2.71.0 |
| `pydantic-graph` | 2.71.0 | 2.71.0 |
| `pydantic-evals` | 2.71.0 | – |
| `pydantic-ai-harness` | 0.71.0 | 0.71.0 |

Python 3.13.18. The installed tool is `uv`.

Source trees I read:

- **pydantic-ai** at `463a6d519b7e4c8e038d5a2559b0d26281df6937` (2026-11-22). Base URL for links: `https://github.com/pydantic/pydantic-ai/blob/463a6d519b7e4c8e038d5a2559b0d26281df6937/`.
  - Since harness v0.36+, **the harness lives inside this repo** at `src/pydantic_ai_harness/`. Its docs are in `docs/harness/`.
  - HEAD may be slightly ahead of the 2.71.0 release. Every behaviour below that I ran was run against the installed 2.71.0 / 0.71.0.
- **pydantic-ai-harness** (the standalone repo) at `4399efacd4755ff6feccc727d3180a97428e76cf` (2026-10-02). Its README says: *"This repository keeps the history up to v0.36.0 and is no longer updated."* It is archival only. Current harness code is `pydantic-ai/src/pydantic_ai_harness`.

### Big context shifts relative to older (v1) Pydantic AI knowledge

These are **VERIFIED** by reading the code.

1. **`pydantic_graph.persistence` no longer exists.** That includes `BaseStatePersistence`, `FileStatePersistence`, `SimpleStatePersistence`, `FullStatePersistence`, `iter_from_persistence`, `load_next`, and the snapshot statuses `created/pending/running/success/error`. A grep for these names in both repos returns nothing.
   - `docs/changelog.md:144`: *"The `pydantic_graph.persistence` package and the `pydantic_graph.mermaid` module are removed … The builder API deliberately does not snapshot graph state, so there is no `pydantic_graph` replacement for the persistence package; to save, resume, and fork **agent run** state, Pydantic AI Harness ships the `StepPersistence` capability."* (PR #5470)
   - `docs/migration.md:153` says the same.
   - `pydantic_graph/pydantic_graph/__init__.py` now exports only builder and graph primitives: `GraphBuilder`, `Graph`, `GraphRun`, `BaseNode`, `End`, `Step`, `Join`, and so on.
   - Even in v1, the `Agent` was never driven by graph persistence. In v2 the question is moot.
2. **`StepPersistence` exists.** It is a harness capability: `pydantic_ai_harness.StepPersistence`, source in `src/pydantic_ai_harness/pydantic_ai_harness/step_persistence/`. It ships:
   - snapshots of the message history,
   - an append-only event log,
   - a **tool-effect ledger** with statuses `started`, `completed` and `failed`, keyed by `(run_id, tool_call_id)`.
3. **New core persistence primitives:**
   - `Conversation` / `ConversationTypeAdapter`.
   - `ConversationStore`, which saves with optimistic concurrency: a `revision` and a `ConversationConflict` error.
   - `capture_run_conversation`.
   - Message `state` fields: `ModelRequest.state` is one of `'complete' | 'interrupted'`, and `ModelResponse.state` is one of `'complete' | 'incomplete' | 'suspended' | 'interrupted'`.
   - `repair_messages()`.
4. **Durable execution is a capability on an ordinary `Agent`:** `TemporalDurability`, `DBOSDurability` and `PrefectDurability` in core. The harness adds AWS Lambda, Absurd, pgtask and Azure Durable Task. Restate and Airflow ship in the engines' own packages. There is also a stable backend builder, documented in `docs/durable_execution/backends.md`.

---

## 1. The agent run lifecycle

**VERIFIED** in `pydantic_ai_slim/pydantic_ai/_agent_graph.py`.

- **Nodes.** The run is a `pydantic_graph` graph with these nodes, all subclasses of `AgentNode`:
  - `UserPromptNode` (L1263),
  - `ModelRequestNode` (L2572),
  - `CallToolsNode` (L4231),
  - `SetFinalResult` (L4766),
  - `End`.
- **`agent.iter()`** yields an `AgentRun`. You can step through it in two ways:
  - `async for node in run:`, or
  - `node = await run.next(node)`, checking `Agent.is_model_request_node(node)`, `Agent.is_call_tools_node(node)`, and so on.
- **Where tools execute.** `CallToolsNode.run` calls into `_tool_execution.py`, which calls `ToolManager.execute_tool_call` in `tool_manager.py`, which goes through the capability hooks `before_tool_execute`, `wrap_tool_execute`, `after_tool_execute` and `on_tool_execute_error`, and finally reaches `toolset.call_tool`.
- **How calls and results are represented.**
  - The model emits `ToolCallPart(tool_name, args, tool_call_id)` inside a `ModelResponse`.
  - Results come back as `ToolReturnPart(..., outcome='success'|'failed'|'denied'|'interrupted'|'retried')`. The `RetryPromptPart` family also exists.
  - These results are carried in the **next** `ModelRequest`.
- **Concurrency.** `ParallelExecutionMode = Literal['parallel', 'sequential', 'parallel_ordered_events']` (`tool_manager.py:51`). The default is `'parallel'`, implemented with `asyncio.create_task` per call (`_tool_execution.py:1036`, `1474`).
  - Per tool, set `sequential=True` to make that tool a barrier.
  - For a whole run, use `ToolManager.parallel_execution_mode('sequential')`.
- **`ToolDefinition` fields** (from the installed 2.71.0): `name`, `parameters_json_schema`, `description`, `outer_typed_dict_key`, `strict`, `sequential`, `kind`, `metadata`, `timeout`, `durable`, `defer_loading`, `unless_native`, `with_native`, `tool_kind`, `unless_tool_kind`, `return_schema`, `include_return_schema`, `toolset_id`, `capability_id`.
  - There is **no `idempotent` / `side_effect_free` field**. Issue #7955 and PR #6344 propose one; both are open.
- **IDs.**
  - `tool_call_id` comes from the persisted `ModelResponse`. It is therefore **stable across a resume that keeps that response** (VERIFIED in exp1 B and exp6).
  - `ctx.run_id` is **new on every `Agent.run`**. Core *refuses* a `run_id` that already appears in `message_history`: *"`run_id=…` already appears in `message_history`. Each agent run needs a distinct `run_id`…"* (VERIFIED in exp6).
  - The `run_id` of the original run stays stamped on the stored `ModelResponse.run_id`.
- **Deferred tools and human-in-the-loop.**
  - A tool raises `ApprovalRequired` or `CallDeferred`, or is declared with `requires_approval=True`.
  - The run then ends with a `DeferredToolRequests` output.
  - You resume with `agent.run(message_history=..., deferred_tool_results=DeferredToolResults(approvals={...}, calls={...}))`.
  - `DeferredToolResults(calls={id: value})` also works for a dangling call that was **never deferred**. The tool is not executed; the value becomes its result (VERIFIED in exp1 I). This is a usable reconciliation hook.
- **Retries.** `ModelRetry` produces a retry return; `max_retries`/`retries=` bound it. Under StepPersistence, a call that ends in `ModelRetry` or `ToolFailed` is recorded `failed` (PR #12638 / #12620, merged).

## 2. What happens on resume from persisted history: the critical point

### 2.1 What the code does

**VERIFIED** in `_agent_graph.py:1286-1385`, `UserPromptNode.run`:

- **History ends in a `ModelResponse` with tool calls, and no new prompt.** The response is adopted and `CallToolsNode(last_message)` is returned. **The pending tool calls are executed (again).**
- **The same history, plus a new `user_prompt`.** It raises `UserError` from `_new_prompt_on_unprocessed_calls_error()` (L1563), with this message:
  *"Cannot provide a new user prompt when the message history contains unprocessed tool calls. Run the agent with this message history and no new prompt to execute the calls, pass `deferred_tool_results` if they were deferred, or call `pydantic_ai.messages.repair_messages()` on the history first to close them out."*
- **History ends in a `ModelRequest(state='interrupted')`.** An in-process exception or cancellation during tool execution leaves this behind: `CallToolsNode` appends it in an `except BaseException:` block (around L4625-4645). Then `_repair_interrupted_tail` (L1534) gives every unanswered call a synthesized `ToolReturnPart(outcome='interrupted', content='The tool call was interrupted before a result was produced.', metadata={'pydantic_ai_synthesized_tool_return': True})`. The tool is **not** re-executed. The model is asked again.
- **History ends in a `ModelResponse(state='interrupted')`.** Without a prompt, its calls are executed. With a prompt, they are closed out as interrupted.
- **Deferred pause.** A history that ends with the requests a deferred pause wrote, marked with reserved `__pydantic_ai__` metadata, re-runs only the calls still waiting (`_answered_at_pause`, L1572).
- **Calls already answered in history are not re-run** (`CallToolsNode._answered_calls`).

### 2.2 Experiment exp1 results

Run in-process, JSON round-tripped through `ModelMessagesTypeAdapter`, pydantic-ai 2.71.0. Full output is in `research-experiments/exp1_output.txt`.

| Case: stored history tail → resume call | Behaviour | Extra side effects |
|---|---|---|
| B: `[Request(prompt), Response(ToolCallPart 'call-1')]` → `run(message_history=h)` | **tool re-executed**, same `tool_call_id='call-1'`, **new `run_id`** | **+1** |
| C: same → `run('new prompt', message_history=h)` | `UserError` (text above) | 0 |
| D: `repair_messages(h)` → `run('new prompt', ...)` | synthesized `outcome=interrupted` return; model continues | 0 |
| E: `h + [ModelRequest(parts=[], state='interrupted')]` → `run(message_history=...)` | synthesized `interrupted` return; tool **not** run | 0 |
| F: response `state='interrupted'` → no prompt | tool executed | +1 |
| G: response `state='interrupted'` → new prompt | closed as interrupted | 0 |
| H: `run(conversation=Conversation(messages=h))` | tool re-executed (same as B) | +1 |
| I: `run(message_history=h, deferred_tool_results=DeferredToolResults(calls={'call-1': 'reconciled'}))` | tool skipped; supplied value used as the result | 0 |
| J: complete finished history → no prompt | no model call, no tool call, returns the existing output | 0 |

### 2.3 In-process interruption (exp2, `research-experiments/exp2_output.txt`)

I raised a `RuntimeError`, called `task.cancel()`, raised `KeyboardInterrupt`, and fired `CancellationToken.cancel()`, each one *after* the side effect. In every case `capture_run_messages()` ends with `[..., ModelResponse(complete, tool-call), ModelRequest(state='interrupted', parts=[])]`.

- Resuming with no prompt closes the call as `interrupted`, and the tool is not re-run.
- `KeyboardInterrupt` raised inside a tool: the tool runs in an `asyncio` task, so the run sees `CancelledError`, and then the `KeyboardInterrupt` escapes `asyncio.run()`. This is standard asyncio behaviour, not something Pydantic AI does.
- `CancellationToken` raises `RunCancelled`, which carries `all_messages()`.
- Existing tests cover this: `tests/test_run_cancellation.py` has 94 tests, including `test_cancel_during_only_tool_call_is_resumable(_without_a_new_prompt)` and `test_run_sync_keyboard_interrupt_carries_run_state`.

**The catch (exp5, VERIFIED mechanics, INFERRED realism).** The synthesized `interrupted` return hands the decision to the **model**. A model that reasons "the payment may not have gone through, retry" issues a *new* call, `call-2`. The result is a duplicate charge, and no framework layer can catch it, because it is a new `tool_call_id`.

Issue #12397 (open, filed for DouweM) reports a related problem: the interrupted result is sent to providers as an ordinary success result, not through the error flag.

### 2.4 Real SIGKILL across process boundaries (exp4 / exp4b)

**VERIFIED.** Full results are in `research-experiments/exp4_output.txt` and `exp4_results.json`.

Set-up:

- The child process runs the agent. The parent sends `SIGKILL` when it sees a marker. A fresh child then resumes.
- The "provider" is a JSONL file, used as the source of truth for counting charges.

Resume strategies:

- **naive**: resume through `message_history`, no prompt.
- **idem**: the provider dedupes on `key=ctx.tool_call_id`.
- **reconcile**: before resuming, look the dangling call's `tool_call_id` up in the provider and supply the result through `deferred_tool_results`.

Persistence modes:

- **naive_iter**: save `run.all_messages()` after every `run.next()`.
- **folded_iter**: also include `node.request` when the next node is a `ModelRequestNode`.
- **step**: `StepPersistence(FileStepStore)` with defaults.
- **step_frontier**: `StepPersistence(..., capture_frontier=True)`.

Results (charges, where 1 is correct):

| kill point → | before effect | after effect, before return | after tool node, before save | during next model request |
|---|---|---|---|---|
| naive_iter + naive | 1 OK | **2 DUP** | **2 DUP** | **2 DUP** (tool return not yet in `all_messages()`) |
| folded_iter + naive | 1 | **2 DUP** | **2 DUP** | 1 OK |
| *_iter + idem / reconcile | 1 | 1 | 1 | 1 |
| step (default) + naive | 1 (no snapshot → `LookupError`, app restarts) | **2 DUP** | n/a | 1 |
| step_frontier + `include_interrupted=True` + naive | 1 | **2 DUP** | n/a | 1 |
| step_frontier + `include_interrupted=True` + idem/reconcile | 1 | 1 | n/a | 1 |

**Finding exp3 / VERIFIED.** When `agent.iter` yields the `ModelRequestNode` that follows a `CallToolsNode`, the tool returns are in `node.request` only. They are **not** in `run.all_messages()` yet.

- So "save `all_messages()` at every node" leaves a window as long as the whole next model request, during which a crash re-executes the tool.
- StepPersistence fixed exactly this for itself (harness #373, closed; see the `after_node_run` docstring, `_capability.py:712`).

**Finding exp4b / VERIFIED.** With `RANDOM_IDS=1`, the model mints a new `tool_call_id` on each response, as real providers do.

- **Idempotency keyed on `tool_call_id` only works if the resume path keeps the persisted `ModelResponse`.**
- Resuming from StepPersistence's *default* (`complete`) snapshot, or restarting when there is no snapshot, **asks the model again**. The model gives a new id, and you get a **DUPLICATE** even with the "idempotent" tool.
- Only `include_interrupted=True`, which keeps the dangling response, gave a correct result.

## 3. Persistence: what each layer guarantees

| Layer | When state is written | Guarantee after `SIGKILL` | Source |
|---|---|---|---|
| Plain `Agent` | Never. Your app persists `result.all_messages()`, `result.conversation`, `capture_run_messages`, or does it per node via `iter`. | Whatever you saved. Resuming re-executes dangling calls. | VERIFIED (exp1/3/4) |
| `ConversationStore` | `save()` after the run (or after an error, via `error.conversation`) | Detects concurrent saves (`ConversationConflict`). Docs: *"the conflict doesn't undo the model requests or tool calls the run already made, so run the turn again only if repeating them is safe. For a run whose tool calls must happen exactly once, see durable execution."* (`docs/persistence.md:111`) | DOCUMENTED |
| Harness `StepPersistence` | Ledger `started` in `before_tool_execute` (*before* the tool body runs); `completed`/`failed` after it; snapshot in `after_node_run` after a settled `CallToolsNode` (with tool returns folded in); with `capture_frontier=True` also before model requests and at the response frontier; `on_run_error` for in-process failures only | `started` with no terminal record means *unknown_after_crash*. The default read path returns only `complete` snapshots. There is a window between `completed` in the ledger and the snapshot. | VERIFIED (`_capability.py:615-753`, `recovery.py`, exp4) |
| Durable engines | The engine journals each model request and each wrapped tool call | A completed unit replays from the journal. The in-flight unit is retried, so it is at-least-once. | DOCUMENTED (below) |

**What `StepPersistence` itself says.** Its docs (`docs/harness/step-persistence.md`) state the limits plainly:

- L83: *"A step that was in flight is not retried: its tool calls show up as `started` effects in the ledger, and you decide whether redoing them is safe."*
- L195: *"An `interrupted` snapshot is sendable, but not necessarily safe: a pending tool call may be re-executed or closed out with a synthesized `interrupted` return, and neither says whether the original side effect happened. … Resuming without a new prompt executes the pending calls. With a new prompt, calls are closed out if some of their batch already returned; if none did, the run raises `UserError`…"* My experiments match this.
- L313: *"Side-effect deduplication is the orchestrator's responsibility."* `annotate_tool_effect(store, ctx, idempotency_key=..., effect_summary=...)` only **records** metadata. It does not dedupe.
- L724-729: *"Automatic execution recovery is not implemented. Snapshots are saved per settled batch, not per tool, so a hard kill during a parallel batch can leave a completed effect with no persisted result. A `started` effect is unknown after a crash, and a `failed` tool may have made partial external changes. … tools with external effects need their own idempotency or reconciliation strategy."*
- `recovery.py` docstring: *"Recovery facts, not replay permission. External side effects require reconciliation."* `inspect_recovery()` returns `latest`, `settled`, `unresolved`, `completed_tools` and `failed_tools`.

**Core persistence docs.** `docs/persistence.md:167`: *"Nothing here snapshots state in the middle of a step … For a run that must survive a crash while it is executing, that is what durable execution is for."*

**A misleading doc sentence.** `docs/agent.md:2292` says: *"pass the captured messages as `message_history` and no new prompt. The new run picks up from the last request, so tool calls that already ran, and any side effects they had, aren't repeated."*

- This is true for calls whose results made it into the captured history.
- It does **not** hold for a call whose result was never persisted (exp1 B, exp4).
- The surrounding text is about in-process errors, where the interrupted `ModelRequest` *is* captured. This is INFERRED to be the intended scope.

## 4. Durable execution: what the docs claim

All of this is **DOCUMENTED**, with short quotes.

- **Overview** (`docs/durable_execution/overview.md:9`): *"A durable engine journals the agent's model requests and the tool calls it wraps, and a restarted run replays the completed ones from the journal and retries the one in flight."*
- **Backend builder** (`backends.md:248-250`): *"A durable unit can run more than once when a worker fails after its side effect but before the checkpoint commits. Tools and decorated capability operations should therefore be idempotent unless the engine provides a suitable at-most-once mode."*
- **Temporal** (`temporal.md:23`): *"if an activity fails part-way through it is restarted from the beginning."*
  - Function tools, MCP and model requests become activities (`temporal.md:75`).
  - Default `RetryPolicy.maximum_attempts=0` means unbounded retries (`temporal.md:909`).
  - Event handlers *"may run more than once … keep its side effects idempotent"* (`:414`).
- **DBOS** (`dbos.md:52-57`): *"Custom tool functions … are **not automatically wrapped** by DBOS … If they involve non-deterministic behavior or perform I/O, you should explicitly decorate them with `@DBOS.step`."*
  - So **a plain function tool re-runs on DBOS recovery**. The test `tests/durable_exec/test_dbos.py:test_dbos_default_ids_survive_recovery` comments: *"the function tool, which runs in the workflow, runs again and sees the same IDs."* That is VERIFIED by reading the test.
  - Issue #11377 (open) asks the docs to cover the side-effecting-tool and idempotency-key pattern: `DBOS.workflow_id` plus `ctx.tool_call_id`. PR #11731 is open.
- **Prefect** (`prefect.md:16,445`): *"This doesn't make external side effects exactly-once"*; *"A task interrupted before its result was recorded runs again, even if its external side effect already happened, so side-effecting tools should be idempotent."* This wording comes from PR #10533 (merged), which *"stop[s] promising exactly-once side effects"*.
- **Restate** (`restate.md:23`): *"A step interrupted before its result was journaled runs again on retry, even if its external side effect already happened. Make side-effecting tools idempotent, for example by passing an idempotency key to the external service."* (PR #10534)
- **Airflow** (`airflow.md:9-15`): an Airflow task plus a step cache. A retry replays the cached model and tool steps.
- **`THREAT_MODEL.md:150`**: *"Persistence also does not roll back an external side effect after a crash."*

**Summary: the application's responsibility, as documented across every engine.** Make side-effecting tools idempotent, typically with a key passed to the external system, or reconcile against the external system. No engine offers exactly-once delivery of a tool's external effect. DBOS function tools are not even wrapped unless you decorate them.

## 5. Observability and evals

These could serve as measurement tools.

- **`Instrumentation` / Logfire spans.** "running tool" spans carry `gen_ai.tool.call.id` (`capabilities/instrumentation.py:619`). Spans are in-memory until exported, so a `SIGKILL` loses unexported spans (INFERRED). They are useful for in-process runs; for a crash harness, an append-only fsync'd effect log is more reliable.
- **`pydantic_evals`.** `Dataset`, `Evaluator` and span-based evaluators: `HasMatchingSpan` (`pydantic_evals/evaluators/common.py:362`) and `SpanTree` (`otel/span_tree.py:491`). A crash scenario could be expressed as an eval case whose evaluator checks the effect count.
- **Open gaps in this area:**
  - #13115 (open, `needs-maintainer-discussion`): *"Evaluating an agent with real tools risks real side effects; there's no built-in way to block, record or replay tool calls in pydantic-evals or the Harness."*
  - #11757 (open): `SimulatedToolset` for tools with side effects.

## 6. Testing infrastructure: a deterministic agent with zero network

**VERIFIED** with exp0 and `research-experiments/exp_common.py`.

```python
import pydantic_ai.models
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import FunctionModel, AgentInfo

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False          # blocks any non-test model

def model(messages: list, info: AgentInfo) -> ModelResponse:   # FunctionModel(function=...)
    done = any(isinstance(p, ToolReturnPart) for m in messages if isinstance(m, ModelRequest) for p in m.parts)
    if not done:
        return ModelResponse(parts=[ToolCallPart('charge', {'amount_cents': 500}, tool_call_id='call-1')])
    return ModelResponse(parts=[TextPart('done')])

agent = Agent(FunctionModel(model))

@agent.tool
async def charge(ctx: RunContext[None], amount_cents: int) -> str:
    return f'charged {amount_cents} for {ctx.tool_call_id}'

print(agent.run_sync('pay').output)  # 'done'
```

Signatures and APIs, verified by import in 2.71.0:

- `FunctionModel(function=None, *, stream_function=None, model_name=None, profile=None, settings=None)`.
  - The function signature is `(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse`.
  - `AgentInfo` has the fields `function_tools`, `allow_text_output`, `output_tools`, `model_settings`, `model_request_parameters` and `instructions`.
- `ToolCallPart(tool_name, args=None, tool_call_id=<factory>, ...)`.
- `ToolReturnPart(tool_name, content, tool_call_id, ..., outcome='success')`.
- `ModelResponse(parts, *, ..., run_id, conversation_id, state='complete')`.
- `ModelRequest(parts, ..., state='complete')`.
- `Agent.run`/`iter(user_prompt=None, *, conversation=None, message_history=None, deferred_tool_results=None, conversation_id=None, run_id=None, ..., cancellation_token=None, timeout=None, capabilities=None, ...)`.
- `RunContext.tool_call_id`, `RunContext.run_id`, `RunContext.tool_call_approved`.
- `result.all_messages()`, `result.new_messages()`, `result.conversation`.
- `ModelMessagesTypeAdapter.dump_json`/`.validate_json`, and `ConversationTypeAdapter`.
- `capture_run_messages()`, `capture_run_conversation()`.
- `repair_messages(messages, *, repair_last_response=True)`.
- `TestModel(call_tools=[...])`.
- `DeferredToolResults(approvals=..., calls=...)` and `DeferredToolRequests`.
- Harness: `StepPersistence(store=..., run_id=..., agent_name=..., capture_frontier=False)`, plus `FileStepStore`, `SqliteStepStore`, `InMemoryStepStore`, `continue_run`, `fork_run`, `annotate_tool_effect`, `step_persistence.recovery.inspect_recovery`.
- Set `PYDANTIC_AI_NO_BANNER=1` to silence the startup banner on stdout. That matters for marker-based subprocess control.

## 7. Existing failure-injection and crash tests

**VERIFIED** with grep and by reading the tests.

- `tests/harness/step_persistence/test_frontier.py::test_hard_kill_leaves_model_frontier_and_unknown_effects` is a **real subprocess `process.kill()`** during a tool. It asserts that an `interrupted` snapshot exists and that there is one unresolved effect. It is marked `@pytest.mark.subprocess(reason='hard-kills a process mid-run …')`.
- `tests/harness/step_persistence/test_step_persistence.py`:
  - `TestCrashMidToolCallContract` (around L1445) simulates a crash by calling `before_tool_execute` directly.
  - `test_crash_mid_tool_cycle_rescues_completed_cycles_as_interrupted` (L2328).
  - `test_crash_leaving_dangling_tool_call_saves_only_an_interrupted_snapshot` (L1594).
- `tests/durable_exec/test_dbos.py` uses `class _ProcessCrash(BaseException)` (L6537) to stand in for process death, then calls `DBOS.resume_workflow_async`. Its comment: *"A unit test because a real recovery would need to kill and restart the process mid-workflow"* (L3337).
- `tests/test_run_cancellation.py` has 94 tests: cancellation and `KeyboardInterrupt` resumability.
- **There is no cross-strategy matrix** that measures duplicate or missing effects under kill points × resume mechanisms × idempotency strategies. Nothing in the upstream tests measures the "model re-issues after `interrupted`" path (exp5) or the "default snapshot re-asks the model and gets a new `tool_call_id`" path (exp4b).
- **Upstream test policy** (`tests/AGENTS.md`, "Test cost"): integration tests mean *"real in-process code paths with recorded requests, never process spawns, real waits"*. A subprocess launch needs `@pytest.mark.subprocess(reason=...)` and is allowed only *"when the claim depends on the process boundary"*.
  - A SIGKILL-matrix suite would therefore **not be welcome as an upstream test suite** at scale (INFERRED from the policy).
  - One targeted subprocess test per claim is allowed.

## 8. GitHub survey

I searched issues and PRs through the REST `search/issues` API. Raw output is in `research-experiments/gh_search_raw.txt` and `gh_issue_bodies_*.txt`. I read comment threads with WebFetch; the `repos/.../issues` API was not enabled for this session. I only report what appears there.

**Directly on point:**

- **#10926** (open, 2026-10-21, labels `question, pkg:core, p:3-mid`, assigned to dsfaccini by the triage bot): *"Approved tool runs again if the process dies after it executed but before the history was saved."*
  - An external user's FunctionModel repro found 2 payments in 3 of 3 runs, on 2.59.0. I **re-confirmed it on 2.71.0** (exp6: two executions, same `tool_call_id` `c1`, different `run_id`s).
  - The triage bot `pydanty` reproduced it on main and wrote: *"nothing settles whether it is intended."*
  - A commenter proposed `ctx.tool_call_id` as the idempotency key. The author measured that this works against a key-honouring API, and offered a docs PR *"only if a maintainer says it is wanted."*
  - The WebFetch view showed **no maintainer comment**. The API reports 11 comments and WebFetch saw 4, so some may be hidden. Not verified.
- **#11951** (open, 2026-10-30, by adtyavrdhn, an org MEMBER): *"Harness Shell: continuing a conversation after a crash runs the interrupted command again."*
  - The cause is a job key of `sha256(run_id:tool_call_id)`, where `run_id` changes on resume.
  - Fix PR **#11960** (open, by the same member) re-keys the job on the `run_id`+`timestamp` of the `ModelResponse` that made the call, plus `tool_call_id` and the command.
  - This is the same class of bug as exp4b, inside the harness itself.
- **#7176** (open, 2026-08-05): *"Capability hooks that touch external state cannot commit exactly once under durable execution."*
- **#12397** (open, bug, filed for DouweM): *"Interrupted tool calls look like results"*. The synthesized `interrupted` return is sent as an ordinary result.
- **#8917** (open, member): *"Durable retries can create a second sandbox after a lost create reply"*. It calls for *"a stable per-run key from core that providers use as a name or idempotency key."*
- **#11377** (open, docs) and **PR #11731** (open): DBOS side-effecting tools and idempotency keys.
- **#12672** (open): *"StepPersistence leaves a tool call skipped with `SkipToolExecution` at `started`."*
- **PRs #10533 and #10534** (merged): removed "exactly-once" promises from the Prefect and Restate docs.
- **PR #12301** (open, docs, by DouweM): *"Going to Production: start from how a run begins and what a crash costs"*. It says a worker retry *"repeats tool side effects"*.

**Related design threads:**

- #9165 "Execution Checkpointing & Resume" (open, member).
- #7247 "First-class checkpoint/rewind primitive" (open, `p:4-low`).
- harness #149 "GraphAgentState serialization + resume_from_state()" (closed; WebFetch showed no closure rationale).
- harness #373 "StepPersistence cannot resume after completed tool-call boundary" (closed, fixed).
- #7955 and PR #6344: `idempotent` / `side_effect_free` tool annotations (open).
- #13115 and #11757: blocked, recorded and simulated side-effecting tools for evals (open).
- #4679 "Agentic two-phase commits" (open).
- #13781 and #7971: carrying a durable unit's side effects in its recorded output (open).

**Searches that returned nothing relevant:**

- `"fault injection"`: one unrelated hit.
- harness repo `idempot`: 0 results.
- harness #814, an "action-recovery integration" invitation, is a withdrawn third-party solicitation and not maintainer interest.

**I found no maintainer statement endorsing an external crash-testing toolkit.**

## 9. Contribution policy

**VERIFIED**: `CONTRIBUTING.md` (identical to `docs/contributing.md`) and `AGENTS.md`.

- *"Want a feature or API change? Open an issue describing the problem you're solving. Do not start with code."*
- *"Make sure a maintainer has agreed to the approach on the issue and assigned you. Then open a PR."*
- *"Unassigned PRs may be auto-closed."*
- *"We treat contributed code as illustrative … we lean toward rewriting contributed code."*
- *"Most new agent behaviors belong in Pydantic AI Harness."* Capabilities can also ship as your own `pydantic-ai-<name>` package.
- A *"draft PR with just a `PLAN.md`"* is the sanctioned proposal form.
- **On AI-generated content.** `AGENTS.md` says:
  - *"overly lengthy AI-generated issues, comments, and proposals are less likely to be helpful and more likely to be ignored than a user's attempt at explaining what they want in their own (possibly translated) words"*;
  - *"Never add yourself (Claude) as a co-author on commits."*
- **Bug fixes** must be *"the narrowest implementation change … often one line plus one regression test."* The maintainers *"may generate a fix internally — the most valuable thing you can do is file a clear report and then validate that the fix works."*
- **Tests:** see §7. Subprocess tests need a marker and a reason tied to the process boundary.
- **Champions model:** *"We prioritize features where one or more champions with production use cases have stepped up."*

---

## 10. Answers to the engineering question

1. **What Pydantic AI handles (VERIFIED):**
   - It makes any history provider-valid, by synthesizing `interrupted` returns and dropping orphans.
   - Calls already answered in history are never re-executed.
   - In-process failure, cancellation or `KeyboardInterrupt` leaves an explicit `ModelRequest(state='interrupted')`, so a resume does not re-execute and is resumable.
   - A new prompt on top of open calls is refused rather than silently abandoning them.
   - Deferred and approved calls resume correctly.
   - `tool_call_id` is stable whenever the persisted response is reused.
2. **What persistence guarantees.** Core: nothing automatic. The app persists the history.
   - StepPersistence: snapshots at settled boundaries, plus a ledger that tells "possibly ran" (`started`) apart from completed or failed. It explicitly does **not** dedupe or auto-recover.
   - Durable engines: replay completed units, at-least-once for the unit in flight.
3. **How side effects are represented.** As `ToolCallPart` and `ToolReturnPart` pairs in messages, plus (in the harness) `ToolEffectRecord(tool_call_id, tool_name, run_id, status, idempotency_key, effect_summary)`. There is no core concept of an "effect" or of idempotency on `ToolDefinition`.
4. **Interruption behaviour.** See §2.3. A hard kill writes nothing (VERIFIED).
5. **Supported resumption mechanisms for a plain `Agent`.** These are the exact ones to build crash tests against.
   - (a) `agent.run(message_history=persisted)` with no prompt. It executes dangling calls.
   - (b) `agent.run(conversation=persisted_conversation)`. Same as (a), and it carries usage and deferred requests.
   - (c) `agent.run(message_history=..., deferred_tool_results=DeferredToolResults(approvals=/calls=))`. This works for approvals, and also to inject a reconciled result for a dangling non-deferred call.
   - (d) `repair_messages(history)` followed by a new prompt. Calls are abandoned as `interrupted`.
   - (e) Harness `continue_run` / `fork_run` (with `include_interrupted=` and `inspect_recovery`), feeding (a).
   - (f) A durable engine's own recovery: Temporal replay, `DBOS.resume_workflow_async` / recovery, a Prefect flow retry.
   - There is no "resume from graph snapshot" for `Agent`.
6. **What remains the application's responsibility.**
   - Idempotency keys that survive resume. Use `tool_call_id`, plus the `run_id` and `timestamp` of the `ModelResponse` that made the call (#11960's approach). Do **not** use `ctx.run_id`.
   - Reconciliation of `started` / dangling calls against the external system.
   - Persisting tool returns promptly, including `node.request` when using `iter`.
   - Choosing a resume path that does not re-ask the model when an effect may have happened.
   - Guarding against the model re-issuing a call after an `interrupted` result.
   - Wrapping DBOS function tools in `@DBOS.step`.
7. **Do failure-injection tests exist?** Yes, a few, focused on StepPersistence's recording contract and DBOS ID stability (§7). There is no measurement matrix across resume mechanisms and idempotency strategies.

## 11. Recommendation

**Would an independent "Agent Crash Lab" add value? Partly.** The raw facts it would produce are already well covered:

- The docs on every engine and on StepPersistence say *"in-flight tool may run again, make it idempotent"*.
- Upstream already has a hard-kill test.
- #10926 already contains a 44-line repro of the core duplicate.
- My exp4 matrix reproduced the expected outcomes in about 40 lines of child script.

A lab whose headline is "naive retry duplicates, idempotency keys fix it" would be **redundant**, and it would conflict with upstream test-cost policy if proposed as a test suite.

**What is new and valuable** is surfaced by these experiments and not covered by the docs:

1. **Persisting `all_messages()` per `iter` node is unsafe.** The tool return lives in `node.request` until the next model request finishes (exp3, exp4 "next_model_request" column). Users following the obvious pattern get duplicates. #11951's repro used exactly this pattern.
2. **`tool_call_id` idempotency breaks on resume paths that re-ask the model.** These include StepPersistence's default `complete`-only `continue_run` after a kill mid-first-cycle, and restarting when no snapshot exists. The model gives a new id, so the effect is duplicated even with keys (exp4b).
3. **`ctx.run_id`-based keys always break on resume**, because `run_id` is new and reuse is refused (exp6, #11951).
4. **The model-level duplicate after a synthesized `interrupted` result** (exp5, related to #12397).
5. **`DeferredToolResults(calls=...)` is an undocumented-but-working reconciliation hook** for dangling non-deferred calls (exp1 I).
6. **The `docs/agent.md:2292` sentence overstates** "side effects aren't repeated".

**Recommended alternative.** It is higher-leverage and fits the contribution policy.

- **(a) Contribute targeted docs and evidence on existing issues, not a toolkit.**
  - Post a short, human-written comment on **#10926** with the 2.71.0 confirmation and the resume-path findings: items 1–3 and 5 above.
  - Offer a docs section, "Crash windows and idempotency keys for side-effecting tools". It would cover which ID to key on (response `run_id` + `tool_call_id`, as in #11960), the `iter` / `node.request` persistence pattern, `include_interrupted` + `inspect_recovery`, and `deferred_tool_results(calls=)` for reconciliation.
  - This aligns with #11377 / #11731 and #12301. Wait for a maintainer to agree before opening a PR.
- **(b) If code is wanted, propose it in the harness as a capability.** Example: an `IdempotentEffects` / reconciliation helper built on the StepPersistence ledger. It would provide a stable effect key exposed to tools, plus a `reconcile(unresolved) -> DeferredToolResults` hook that consults a user-supplied checker before resume.
  - Do this through a feature-request issue or a `PLAN.md` draft first.
  - Related threads: #7955 (tool `idempotent` declarations) and #8917 (core stable per-run key).
- **(c) Keep the Crash Lab as a small external, reproducible benchmark** (its own repo, `pydantic-ai-<name>` style, if packaged), not an upstream test suite. Use it to back (a) and (b) with numbers. Its matrix should be built around the six resumption mechanisms in §10.5, and it should include:
  - persistence pattern (all_messages / folded / StepPersistence default / StepPersistence frontier+interrupted / durable engine);
  - kill points (before effect / after effect / after tool node before save / during next model request / mid-parallel-batch);
  - stable vs fresh `tool_call_id`;
  - a model that does or does not re-issue after `interrupted`;
  - key choice (`run_id`+`tool_call_id` vs response-origin+`tool_call_id`) vs reconciliation.

  The experiment scripts here are a working seed.

## 12. Open questions and unverified points

- I did **not** run the durable-engine claims in a real Temporal, DBOS or Prefect server. The DBOS "function tool re-runs" claim is from its docs and a test comment.
- I did not test a kill in the middle of a *parallel* batch. The docs say it can leave a completed effect with no persisted result. (Measured later by the lab: `mid_parallel_batch` cells.)
- I did not measure the window between the `completed` ledger write and the snapshot. (Measured later by the lab: `after_tool_node` and `during_snapshot_write` cells.)
- #10926's thread: the API counts 11 comments and WebFetch saw 4. A hidden maintainer reply cannot be ruled out.
- `RetryPromptPart` handling on resume was not exercised.
- Whether real provider tool-call ids are ever reused deterministically was not tested. exp4b assumes fresh ids.

## 13. Experiment index (`docs/research-experiments/`)

| Script | Output | What it shows |
|---|---|---|
| `exp0_zero_network.py` | `exp0_output.txt` | TestModel agent with a tool call, no network |
| `exp_common.py` | – | deterministic FunctionModel + side-effect ledger |
| `exp1_resume_dangling.py` | `exp1_output.txt` | resume behaviour for 10 history-tail shapes |
| `exp2_inprocess_interrupt.py` | `exp2_output.txt` | exception / cancel / token / KeyboardInterrupt mid-tool |
| `exp3_iter_boundaries.py` | `exp3_output.txt` | what `iter` exposes at each node; tool returns live in `node.request` |
| `exp4_child.py`, `exp4_sigkill_matrix.py`, `exp4_sigkill_matrix_lib.py` | `exp4_output.txt`, `exp4_results.json` | real SIGKILL × persistence × resume × idempotency matrix |
| `exp4b_random_ids.py` | `exp4b_output.txt` | fresh tool_call_ids break `tool_call_id` keys on paths that re-ask the model |
| `exp5_interrupted_return_model_retry.py` | `exp5_output.txt` | duplicate caused by the model re-issuing after `interrupted` |
| `exp6_api_facts.py` | `exp6_output.txt` | approval re-execution (#10926 on 2.71.0), `run_id` reuse refused, `ToolDefinition` fields |
| – | `gh_search_raw.txt`, `gh_issue_bodies_1.txt`, `gh_issue_bodies_2.txt` | GitHub survey raw data |
