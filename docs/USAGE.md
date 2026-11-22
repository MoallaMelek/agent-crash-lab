# Usage

POSIX only (Linux, macOS): the lab kills its own child processes with `SIGKILL`. Python 3.11 or newer.

## Install

```bash
uv sync            # runtime deps pinned exactly: pydantic-ai-slim 2.71.0, pydantic-ai-harness 0.71.0
uv run crashlab doctor
```

`doctor` prints the versions and checks for POSIX. It then runs the headline duplicate cell once
and confirms that attempt 1 really died with `-SIGKILL`.

## Commands

```bash
uv run crashlab list                        # all curated cells, grouped by finding, with their claims
uv run crashlab list --finding F2 --json

uv run crashlab run H2-naive-history-duplicates            # one cell: timeline, effects, verdict
uv run crashlab run H2-naive-history-duplicates --keep-workdir   # keep fakepay.db, steps.db, timeline.jsonl
uv run crashlab run H2-naive-history-duplicates --set key=business   # change one axis
uv run crashlab run custom --set persistence=step_frontier --set kill=after_effect \
    --set resume=reconcile --set ids=fresh

uv run crashlab matrix -j 4                 # the curated matrix (92 cells), about a minute on 2 cores
uv run crashlab matrix -j 4 --repeat 3 --check --out evidence/$(date +%F)
uv run crashlab matrix --quick              # 15 representative cells
uv run crashlab matrix --finding F4
uv run crashlab matrix --full -j 8 --out /tmp/full   # full cartesian product, unchecked, large

uv run crashlab report evidence/2026-11-22/results.json --out site/
```

## Axes accepted by `--set`

| Axis | Values |
|---|---|
| `scenario` | `single_charge`, `charge_then_notify`, `parallel_batch` |
| `persistence` | `final_only`, `iter_naive`, `iter_folded`, `step`, `step_frontier` |
| `resume` | `restart`, `history`, `continue_default`, `continue_interrupted`, `repair`, `reconcile` |
| `key` | `none`, `run_id+tool_call_id`, `tool_call_id`, `response_origin`, `business` |
| `ids` | `stable`, `fresh` |
| `after_interrupted` | `trust`, `reissue` |
| `kill` | `none`, `before_effect`, `after_effect`, `after_tool_node`, `during_next_model_request`, `mid_parallel_batch`, `during_snapshot_write`, `after_snapshot_write` |
| `crash` | `sigkill`, `exception` (only with `before_effect` or `after_effect`) |
| `kill_tool` | `charge_order`, `notify_customer` |

The `docs/ARCHITECTURE.md` page defines exactly where each kill point fires.

Some combinations cannot be built without faking a mechanism. Those report `NOT_APPLICABLE` with
the reason; nothing is spawned for them.

## Reading a verdict

| Verdict | Meaning |
|---|---|
| `OK` | One effect each, the run completed, and the transcript is consistent with FakePay. |
| `DUPLICATE` | More than one effect. This is a finding, not a failure of the tool. |
| `MISSING` | No effect, but the run completed. |
| `STRANDED` | The recovery attempt raised or timed out. |
| `AMBIGUOUS_RESOLVED` | One effect, but the agent's transcript disagrees: for example, the call was closed as `interrupted`. |
| `KILL_NOT_REACHED` | The configured kill point never fired, so nothing was tested. |
| `NOT_APPLICABLE` | The combination is not meaningful. |
| `LAB_ERROR` | The lab misbehaved. This is never a finding about Pydantic AI. |

Each record also carries three more things:

- an `explanation`, generated from the recorded facts;
- `responsibility` factors, labelled `framework_documented`, `framework_observed`,
  `application_choice`, `provider_assumption` or `scripted_model_behaviour`;
- consistency `checks`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. Findings such as `DUPLICATE` do not fail the command. |
| `1` | `--check` failed: a verdict differs from the curated expectation, a cell is not reproducible across `--repeat`, or a `LAB_ERROR` occurred. |
| `2` | Usage error. |
| `3` | Unsupported platform. |

## Upgrading Pydantic AI

Change the pins in `pyproject.toml`, run `uv lock`, then:

```bash
uv run crashlab matrix -j 4 --repeat 3 --check --out evidence/$(date +%F)
```

Any cell whose verdict changes is listed with `<- expected ...`. Investigate it before you update
its expectation in `src/crashlab/matrix.py`. Then copy the new evidence to
`docs/evidence/latest/`; the Pages workflow publishes the viewer from there.

## Adding a cell

Append a `_c(...)` entry to `CURATED` in `src/crashlab/matrix.py`. It needs:

- a unique `id`;
- a finding group;
- the expected verdict;
- a one-sentence claim;
- the axis values.

Then run `uv run crashlab run <id>`.

## Development

```bash
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                                  # strict, src/ only
uv run pytest -m "not subprocess"            # unit tests, under 2 s
uv run coverage run -m pytest && uv run coverage combine && uv run coverage report
```

Coverage also measures the child processes, through `patch = ["subprocess"]`. Children killed by
SIGKILL cannot write coverage data, by design.
