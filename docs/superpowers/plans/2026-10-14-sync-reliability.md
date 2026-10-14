# Sync Reliability Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current session. Keep the progress checklist current; do not run synchronization against production.

**Goal:** Make all nine synchronization tasks safe to rerun, observable during failure, consistent in preview mode, and backed by shared infrastructure.

**Architecture:** Keep individual CLI entry points and the existing task registry. Share renewable resource leases, guarded archive cleanup, order normalization, and SCS session validation. Validate HANA data in a disk snapshot before mutation; publish dataset versions so the existing Go summary implementation can detect cron updates.

**Tech Stack:** Python, pytest, SQLite, PyMongo, httpx, Go MongoDB driver.

**Spec:** `docs/同步脚本评审-2026-10-14.md`

## Global Constraints

- Preserve pre-existing Go changes and current business filters, source identities, and the fixed 2026 delivery/change datasets.
- Do not execute production synchronization, change crontab, or commit/push.
- Dry-run may read configuration, checkpoints and source data, but must not mutate MongoDB.
- Use existing dependencies and shared APIs; avoid a replacement framework.

## Review Focus

- Partial/empty/invalid source results must not delete valid local records.
- Direct CLI and orchestrator runs must share resource locks; lease loss must stop execution.
- A monthly rebuild must preserve all data outside its window and must not move the incremental checkpoint backwards.
- Preview and apply must use the same source/checkpoint window; buffers must stay bounded.
- Cron completion must invalidate the same materialized summary that API completion refreshes.

## Task 1: Shared Runtime

Files: create `scripts/sync_runtime.py`, `tests/test_sync_reliability.py`; modify shared HANA lock delegation and task locks.

Interfaces: `Lease(db, name, owner, ttl_seconds, heartbeat=None)`, `check_leases()`, `archive_cleanup(db, collection, query, run_id, source_rows, allow_large_cleanup=False)`, `order_key(value)`.

- [x] Add failure tests for lease exceptions, archive failure, empty snapshots and deletion thresholds.
- [x] Run initial regression tests and observe seven expected failures before implementation.
- [x] Implement renewable leases with ownership checks and archive-before-delete batches; use stable resource names across entry points.
- [x] Run focused tests and existing Python tests.

Reference behavior:

```python
if existing_count and not source_rows:
    raise RuntimeError("Empty source snapshot; refusing cleanup")
lease.assert_owned()
archive.bulk_write(archive_operations, ordered=False)
target.delete_many(scoped_query)
```

## Task 2: HANA Snapshot and Preview

Files: HANA shared implementation, `scripts/table_pipeline.py`, delivery entry point and tests.

Interfaces: snapshot statistics contain `snapshot_complete`, `range`, `run_id`; `finalize_view_run` consumes only complete results and cleanup remains scoped to the source range.

- [x] Test partial/empty full cleanup, timestamp lookback, duplicate source conflicts, preview zero writes and checkpoint continuity.
- [x] Stage source rows and hashes in SQLite; reject conflicting source keys before MongoDB writes; commit only after cleanup.
- [x] Make previews read checkpoints without locks/history writes; fix BOM date encoding and collection configuration.
- [x] Remove the second full delivery preflight read; verify against the same staged source.

Reference query:

```python
scope = {"_source_view": spec.view_id}
if spec.start_date_field and start_date:
    scope[spec.start_date_field] = {"$gte": start_date, "$lte": end_date}
```

## Task 3: Orchestrator

Files: `scripts/sync/syncctl.py` and focused subprocess tests.

- [x] Test lost leases, hanging subprocesses, nested transient errors and supervised task heartbeats.
- [x] Use shared leases, continuously heartbeat running records, bound task duration, stream logs, terminate/reap process groups on failure.
- [x] Propagate supported end dates to every task and store clear skipped states after failure.
- [x] Record structured errors and preserve original partial results for retries.

Reference execution:

```python
while child.poll() is None:
    lease.assert_owned()
    store.heartbeat(run_id)
    if time.monotonic() >= deadline:
        terminate_process_group(child)
        raise SyncError("Task deadline exceeded", code="task_timeout")
```

## Task 4: Source and Backfill Consistency

Files: sales, SCS, repair workflow, maintenance and tests.

- [x] Test pagination timeout/login pages, replay without offset skipping, failed details retaining old data, bounded BOM batches and unchanged rows.
- [x] Share SCS login/request validation; reject partial pagination, archive scoped full cleanup and use replay-safe checkpoints.
- [x] Retry pending DOA details outside the incremental window; recompute historical change classification in bounded batches.
- [x] Unify MongoDB URI/BSON/HANA helpers and order normalization; fix preview defaults, date bounds and redundant writes.
- [x] Stream maintenance actions and remove production-unused legacy helpers after updating their tests.

## Task 5: Dataset Versions and Documentation

Files: existing Go summary implementation/main loop, Python dataset version publisher, docs and tests.

- [x] Add opt-in MongoDB tests for source/summary version comparison, unchanged versions, and active writers; preserve existing summary publication behavior.
- [x] Publish delivery dataset version only on successful commit; poll versions in the backend and refresh through the existing Go implementation.
- [x] Update operational documentation and remove obsolete repair watermark/deletion claims.
- [x] Run Python tests, Go tests/vet, static checks, and a final diff review. Record production integration limits.

## Execution Ledger

- Initial state: four Go files already modified by the user; preserve those edits.
- Execution authorized by the user's “设计一个方案，执行优化”; implement locally without a separate approval stop.
- No production synchronization or cron changes are part of implementation verification.
- Shared task definitions now live in `backend/internal/syncmanifest/tasks.json`; Go embeds the file and Python reads it.
- Preview backfill and table cleanup use a read-only disk snapshot of the same validated source rows.
- Removed unused `run_cli`, `watermark_max`, offset-resume helpers and legacy full-list fetch APIs.
- Kept the existing repair implementation module location to preserve callers and monkeypatch contracts; removed duplicated connection, URI, BSON, signing and archive implementations instead.
- Deployment must wait for older sync processes to finish because dataset resource lock names replace script-specific lock names.
- Real MongoDB lease contention, HANA/SCS response compatibility, and the Go version-refresh integration test remain deployment integration checks; this session does not use a production or test MongoDB endpoint.
- Final verification: `.venv-test/bin/python -m pytest -q` passed 140 tests; `env -u MONGO_TEST_URI go test ./...` and `go vet ./...` passed; `git diff --check` passed; `syncctl.py tasks` returned the nine manifest tasks.
- MongoDB integration tests are opt-in and were skipped locally. Existing Go integration coverage now includes version-triggered refresh, unchanged-version reuse and active-writer suppression, for execution against an isolated test database in CI.
- No code was committed or pushed, no server was restarted, and no scheduled or production sync was executed.
