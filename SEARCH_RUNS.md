# Durable search runs and history

Each market search now becomes a durable operating record rather than a result
that disappears when the browser closes. A short human-readable ID is allocated
before provider work begins, progress is checkpointed, and the exact result
snapshot can be reopened from Search History.

## User workflow

```text
define area and categories
  → create S-XXXX-XXXX search ID
  → checkpoint after each completed category
  → finish, stop at budget, fail with saved partial work, or survive restart as interrupted
  → reopen exact snapshot from History
  → copy link, export CSV, or rerun as a new linked search
```

The ID is designed for communication and retrieval, not as an authorization
credential. Access remains controlled by the protected application boundary.

## API surface

| Method and route | Purpose |
|---|---|
| `POST /api/search/run` | Validate the requested geometry, allocate an ID, execute the bounded search, and persist its terminal state. |
| `GET /api/search-runs` | List recent runs; optional query text filters by ID or area label. |
| `GET /api/search-runs/{search_id}` | Return parameters, counters, status, coverage, error detail, and the saved result snapshot. |

A direct browser link uses `/#search=S-XXXX-XXXX`. Rerunning an identical query
receives a new ID and records `repeated_from`, preserving both the operating
event and the relationship to the earlier run.

## Stored model

| Table | Responsibility |
|---|---|
| `search_runs` | ID, normalized parameters, query fingerprint, lifecycle state, counters, timing, coverage, cost estimate, error detail, and repeat relationship. |
| `search_run_results` | Ordered candidate snapshot for reopening, export, and recovery. |

The schema uses a database uniqueness constraint plus bounded retry when
allocating IDs. Query fingerprints are deterministic hashes of normalized
parameters; the short display ID remains random and non-semantic.

## Lifecycle and recovery

| State | Meaning |
|---|---|
| `running` | The run has an ID and may contain one or more checkpoints. |
| `complete` | Every requested category completed within the run budget. |
| `partial` | Usable results exist, but one or more categories did not complete. |
| `stopped_budget` | A configured spend boundary stopped additional provider work. |
| `failed` | The provider or application returned a terminal failure; the failed category, detail, and completed-category results are retained. |
| `interrupted` | Startup recovery found an unfinished run after process termination. |

Checkpoint updates and their result rows are written in one SQLite transaction.
On startup, a prior `running` row becomes `interrupted` without discarding its
checkpointed candidates. A rerun creates a new record; it does not rewrite the
original history.

If a provider fails after an earlier category completed, the response and
browser immediately show the checkpointed candidates, identify the category
that failed, and list categories not completed. The user can inspect, link, or
export that partial work without waiting for a refresh or rerunning the entire
market.

## Acceptance evidence

| Scenario | Verification |
|---|---|
| ID exists before provider work and matches the human format | `SearchRunHistory.test_id_exists_before_provider_work_and_has_a_human_format` |
| Results, geometry, counters, and coverage round-trip | `SearchRunHistory.test_completed_run_round_trips_metrics_and_results` |
| Checkpoint survives and remains reopenable | `SearchRunHistory.test_checkpoint_preserves_a_recoverable_result_snapshot` |
| Process restart preserves partial work | `SearchRunHistory.test_restart_marks_running_work_interrupted_without_losing_results` |
| Repeat receives a new ID linked to its predecessor | `SearchRunHistory.test_an_identical_repeat_links_to_the_previous_run` |
| A provider failure after a completed category returns and persists the checkpointed candidates | `DurableSearchRuns.test_provider_failure_returns_checkpointed_partial_results` |
| Invalid geometry, provider failure, budget stop, and polygon clipping retain correct states | `DurableSearchRuns` tests |
| History rows open by button, mouse, or keyboard; partial results, direct links, filtering, CSV, and zero-call cached reruns work in Chromium | `tests/browser_search_history.py` |

The current implementation is synchronous and single-host, which matches the
protected operating deployment. The durable schema and explicit states provide
the migration seam for queued workers if search volume later requires
asynchronous execution.
