from datetime import date
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from scripts.sources.hana import hana_view_sync as hana
from scripts.sources import scs_doa_sync as doa
from scripts.sync import syncctl
from scripts import sync_runtime as runtime


def test_station_lookback_applies_to_timestamp():
    spec = hana.VIEW_SPECS["Z_V_ZMES_T_001"]
    _, params = hana.build_query(spec, {"watermark": "2026-10-14 10:00:00"}, "incremental", None, 7)
    assert params[0] == "2026-10-07 10:00:00"


def test_station_watermark_compares_mixed_iso_separators():
    spec = hana.VIEW_SPECS["Z_V_ZMES_T_001"]
    assert hana.max_watermark(spec, "2026-10-14T09:00:00",
                              {"ACTUAL_START_TIME": "2026-10-14 10:00:00"}) == "2026-10-14 10:00:00"


def test_sales_nested_transient_error_is_retryable():
    assert syncctl.is_retryable(
        {"success": False, "sources": {"SG": {"success": False, "error": "connection timed out"}}}, 1
    )


def test_doa_pagination_timeout_fails_instead_of_returning_partial(monkeypatch):
    def request(_client, _method, _url, **kwargs):
        if kwargs["params"]["pageNo"] == 2:
            raise doa.httpx.ReadTimeout("partial page")
        return SimpleNamespace(text="page", raise_for_status=lambda: None)

    monkeypatch.setattr(doa, "request", request)
    monkeypatch.setattr(doa, "parse_list", lambda _html: [{"doa_code": "DOA1"}])
    with pytest.raises(doa.httpx.ReadTimeout):
        list(doa.iter_list(object(), page_size=1))


def test_login_page_is_not_an_empty_result():
    with pytest.raises(RuntimeError, match="table|session|页面|表"):
        doa.parse_list("<html><form id='login'>Session expired</form></html>")


@pytest.mark.parametrize("row", [{"declare_time": ""}, {"declare_time": "2025-01-01"}])
def test_scs_snapshot_rejects_invalid_or_out_of_window_rows(row):
    with pytest.raises(RuntimeError):
        doa.validate_scs_row(row, "declare_time", "2026-01-01", "2026-12-31")


def test_doa_backfill_projection_includes_compared_classification():
    db = Mock()
    collection = Mock()
    db.__getitem__ = Mock(return_value=collection)
    collection.find.return_value = []
    doa.backfill_5000_company(db, "doa")
    assert collection.find.call_args.args[1]["is_5000_company"] == 1


def test_end_date_is_forwarded_to_sales_and_scs():
    for name in ("sales_orders", "scs_doa", "scs_change"):
        command = syncctl.TASK_BY_ID[name].command("python", "incremental", None, "2026-09-30")
        assert command[-2:] == ["--end-date", "2026-09-30"]


def test_bom_preview_date_matches_compact_source_encoding():
    from scripts.maintenance import clean_order_boards as boards

    query = boards.base_filter(boards.SPECS["bom"], date(2026, 9, 1), date(2026, 9, 30))
    assert query["BUDAT_MKPF"] == {"$gte": "20260901", "$lt": "20261001"}


def test_renewal_connection_error_marks_lease_lost():
    db = Mock()
    db.__getitem__ = Mock(return_value=Mock())
    lease = runtime.Lease(db, "test")
    lease._stop = SimpleNamespace(wait=lambda _seconds: False)
    lease.collection.update_one.side_effect = RuntimeError("connection timeout")
    lease._renew()
    assert lease.lost
    with pytest.raises(RuntimeError, match="租约"):
        lease.assert_owned()


def test_commit_verifies_expired_or_replaced_owner():
    db = Mock()
    db.__getitem__ = Mock(return_value=Mock())
    lease = runtime.Lease(db, "test")
    lease.collection.find_one.return_value = None
    with pytest.raises(RuntimeError, match="租约"):
        lease.assert_owned(verify=True)


@pytest.mark.parametrize("existing,stale,source", [(1, 1, 0), (500, 400, 100)])
def test_cleanup_guards(existing, stale, source):
    with pytest.raises(RuntimeError):
        runtime.validate_cleanup(existing, stale, source)


def test_cleanup_batches_ids_preserves_scope_and_never_accesses_archive(monkeypatch):
    target = Mock()
    target.count_documents.return_value = 3
    target.find.return_value = iter([{"_id": value} for value in ["a", "b", "c"]])
    target.delete_many.side_effect = [
        SimpleNamespace(deleted_count=2), SimpleNamespace(deleted_count=1),
    ]
    leases = Mock()
    monkeypatch.setattr(runtime, "check_leases", leases)
    query = {"_source_view": "view", "_sync_run_id": {"$ne": "current"}}
    deleted = runtime.cleanup_records(
        {"target": target}, "target", {"_source_view": "view"}, query, 3, batch_size=2,
    )
    assert deleted == 3
    target.find.assert_called_once_with(query, {"_id": 1})
    assert [call.args[0] for call in target.delete_many.call_args_list] == [
        {**query, "_id": {"$in": ["a", "b"]}},
        {**query, "_id": {"$in": ["c"]}},
    ]
    assert leases.call_count == 2
    assert all(call.kwargs == {"verify": True} for call in leases.call_args_list)


@pytest.mark.parametrize("existing,stale,source", [(1, 1, 0), (500, 400, 100)])
def test_cleanup_guard_prevents_deletion(existing, stale, source):
    target = Mock()
    target.count_documents.side_effect = [existing, stale]
    with pytest.raises(RuntimeError):
        runtime.cleanup_records({"target": target}, "target", {}, {}, source)
    target.find.assert_not_called()
    target.delete_many.assert_not_called()


@pytest.mark.parametrize("failure", ["lease", "delete"])
def test_cleanup_failure_propagates_and_closes_cursor(monkeypatch, failure):
    target = Mock()
    target.count_documents.return_value = 1
    cursor = Mock()
    cursor.__iter__ = Mock(return_value=iter([{"_id": "old"}]))
    target.find.return_value = cursor
    if failure == "lease":
        monkeypatch.setattr(runtime, "check_leases", Mock(side_effect=RuntimeError("lease lost")))
    else:
        target.delete_many.side_effect = RuntimeError("delete failed")
    with pytest.raises(RuntimeError, match=failure):
        runtime.cleanup_records({"target": target}, "target", {}, {}, 1)
    cursor.close.assert_called_once()
    if failure == "lease":
        target.delete_many.assert_not_called()


def test_snapshot_accepts_identical_rows_and_rejects_conflicts():
    with runtime.Snapshot() as snapshot:
        snapshot.add("key", {"value": 1})
        snapshot.add("key", {"value": 1})
        assert snapshot.count == 1 and snapshot.duplicates == 1
        with pytest.raises(RuntimeError, match="Conflicting"):
            snapshot.add("key", {"value": 2})


def test_hana_interrupted_read_never_writes_target(monkeypatch):
    db = Mock()
    target = Mock()
    checkpoints = Mock()
    checkpoints.find_one.return_value = None
    db.__getitem__ = Mock(side_effect=lambda name: checkpoints if name == "sync_checkpoints" else target)
    client = Mock()
    client.__getitem__ = Mock(return_value=db)
    monkeypatch.setattr(hana, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(hana, "mongo_uri", lambda: "mongodb://test")
    spec = hana.VIEW_SPECS["ZSGV_ZPP_SERNOLIST"]
    cursor = Mock()
    cursor.fetchmany.side_effect = [[("A", "B", "1", "2", "3")], RuntimeError("HANA disconnected")]
    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(hana, "hana_connection", connection)
    with pytest.raises(RuntimeError, match="disconnected"):
        hana.sync_view(spec, mode="full", start_date=None, batch_size=1,
                       lookback_days=7, dry_run=False)
    target.bulk_write.assert_not_called()
    target.delete_many.assert_not_called()
    target.create_index.assert_not_called()


def test_hana_conflicting_keys_fail_before_any_writes(monkeypatch):
    spec = hana.VIEW_SPECS["ZSGV_ZSD124"]
    row = {field: "same" for field in spec.columns}
    conflicting = {**row, "MENGE": "changed"}
    cursor = Mock()
    cursor.fetchmany.side_effect = [
        [tuple(row[field] for field in spec.columns), tuple(conflicting[field] for field in spec.columns)], [],
    ]
    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=lambda: cursor)
    db = Mock()
    collection = Mock()
    collection.find_one.return_value = None
    db.__getitem__ = Mock(return_value=collection)
    client = Mock()
    client.__getitem__ = Mock(return_value=db)
    monkeypatch.setattr(hana, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(hana, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(hana, "hana_connection", connection)
    with pytest.raises(RuntimeError, match="Conflicting"):
        hana.sync_view(spec, mode="full", start_date="20260101", batch_size=2,
                       lookback_days=7, dry_run=False)
    collection.bulk_write.assert_not_called()
    collection.delete_many.assert_not_called()


def test_table_preview_has_zero_db_writes_and_cleans_new_snapshot(monkeypatch):
    from scripts import table_pipeline as pipeline
    target = Mock()
    target.find_one.return_value = {"watermark": "20261014"}
    db = Mock()
    db.__getitem__ = Mock(return_value=target)
    client = Mock()
    client.__getitem__ = Mock(return_value=db)
    monkeypatch.setattr(pipeline, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(pipeline, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(pipeline, "load_dotenv", lambda: None)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("preview attempted lock mutation")
    monkeypatch.setattr(pipeline, "resource_lock", forbidden)
    monkeypatch.setattr(pipeline, "process_lock", forbidden)
    def staged(_spec, **kwargs):
        assert kwargs["dry_run"]
        kwargs["preview_snapshot"].add("new", {"_id": "new", "_source_view": "ZSGV_ZSD124",
                                               "BUDAT_MKPF": "20261014", "CPX": "other"})
        return {"success": True, "run_id": "preview"}
    monkeypatch.setattr(pipeline, "sync_view", staged)
    args = SimpleNamespace(mode="incremental", start_date=None, end_date=date(2026, 10, 14),
                           batch_size=2, lookback_days=7, apply=False, limit=None, no_progress=True)
    result = pipeline.run_hana_table("ZSGV_ZSD124", "bom", args)
    assert result["success"] and result["cleanup"]["delete_candidates"] == 1
    target.insert_one.assert_not_called()
    target.update_one.assert_not_called()
    target.bulk_write.assert_not_called()


def test_scs_change_failed_page_does_not_write_or_finish_checkpoint(monkeypatch):
    from contextlib import nullcontext
    from scripts.sources import scs_change_sync as change
    target, checkpoints = Mock(), Mock()
    checkpoints.find_one.return_value = {"watermark": "2026-09-01"}
    db = {"scs_change_records": target, "sync_checkpoints": checkpoints}
    client = Mock()
    client.__getitem__ = Mock(return_value=db)
    monkeypatch.setattr(change, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(change, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(change, "load_dotenv", lambda *_args: None)
    monkeypatch.setattr(change, "resource_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(change, "login", lambda *_args: None)
    monkeypatch.setattr(change.httpx, "Client", lambda **_kwargs: nullcontext(object()))
    def fetch(_client, page, *_args):
        if page == 2:
            raise change.httpx.ReadTimeout("page timeout")
        return [{"so_code": "SO-1", "create_time": "2026-09-01"}]
    monkeypatch.setattr(change, "fetch_change_page", fetch)
    with pytest.raises(change.httpx.ReadTimeout):
        change.sync(SimpleNamespace(full=True, start_date="2026-01-01", page_size=1,
                                    lookback_days=2, dry_run=False))
    target.bulk_write.assert_not_called()
    target.delete_many.assert_not_called()
    checkpoints.update_one.assert_not_called()


def test_tasks_manifest_is_unique_valid_and_complete():
    assert len(syncctl.TASKS) == len(syncctl.TASK_BY_ID) == 8
    for task in syncctl.TASKS:
        assert (syncctl.ROOT / task.script).is_file()
        assert all(syncctl.TASK_BY_ID[name].priority < task.priority for name in task.dependencies)


def test_orchestrator_lock_conflict_does_not_abandon_existing_run(monkeypatch):
    store, lease = Mock(), Mock()
    lease.acquire.side_effect = syncctl.SyncError("already running")
    monkeypatch.setattr(syncctl, "RunStore", lambda _db: store)
    monkeypatch.setattr(syncctl, "Lease", lambda *_args: lease)
    with pytest.raises(syncctl.SyncError):
        syncctl.Orchestrator(object()).execute(mode="incremental")
    store.abandon_stale.assert_not_called()
    store.create.assert_not_called()


def test_orchestrator_unexpected_error_finishes_skips_and_releases(monkeypatch):
    store, lease = Mock(), Mock()
    store.run.return_value = None
    lease.lost = False
    monkeypatch.setattr(syncctl, "RunStore", lambda _db: store)
    monkeypatch.setattr(syncctl, "Lease", lambda *_args: lease)
    runner = syncctl.Orchestrator(object())
    monkeypatch.setattr(runner, "_execute_task", Mock(side_effect=RuntimeError("unexpected failure")))
    result = runner.execute(mode="incremental", requested=["station_records"], run_id="test")
    assert result["state"] == "failed"
    store.finish.assert_called_once_with("test", "failed", "unexpected failure")
    assert any(call.kwargs.get("state") == "skipped" and call.args[1] == "station_records"
               for call in store.stage.call_args_list)
    lease.close.assert_called_once()


def test_monthly_hana_commit_preserves_newer_checkpoint_and_scopes_cleanup(monkeypatch):
    checkpoints, target, versions, runs = Mock(), Mock(), Mock(), Mock()
    checkpoints.find_one.return_value = {"watermark": "20261014"}
    db = {"sync_checkpoints": checkpoints, "orders": target, "sync_runs": runs,
          "sync_dataset_versions": versions}
    client = Mock()
    client.__getitem__ = Mock(return_value=db)
    monkeypatch.setattr(hana, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(hana, "mongo_uri", lambda: "mongodb://test")
    cleanup = Mock(return_value=1)
    monkeypatch.setattr(hana, "cleanup_records", cleanup)
    from dataclasses import replace
    spec = replace(hana.VIEW_SPECS["ZSGV_ZSD124"], collection="orders")
    hana.finalize_view_run(spec, {
        "success": True, "snapshot_complete": True, "run_id": "monthly", "mode": "full",
        "watermark": "20260930", "unique_rows": 4, "range": {"start": "20260901", "end": "20260930"},
    })
    assert cleanup.call_args.args[2]["BUDAT_MKPF"] == {"$gte": "20260901", "$lte": "20260930"}
    assert checkpoints.update_one.call_args.args[1]["$set"]["watermark"] == "20261014"


def test_supervisor_terminates_hanging_child_and_heartbeats(tmp_path):
    heartbeats = []
    with pytest.raises(syncctl.SyncError) as raised:
        syncctl.supervise([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1,
                           heartbeat=lambda: heartbeats.append(True), log_prefix=tmp_path / "hung")
    assert raised.value.code == "task_timeout"
    assert len(heartbeats) >= 2


def test_supervisor_reaps_child_after_lease_failure(tmp_path):
    def failed_lease():
        raise RuntimeError("lease lost")
    with pytest.raises(RuntimeError, match="lease lost"):
        syncctl.supervise([sys.executable, "-c", "import time; time.sleep(60)"], timeout=5,
                           heartbeat=failed_lease, log_prefix=tmp_path / "lost")


def test_doa_detail_failure_preserves_prior_fields_and_sets_pending(monkeypatch):
    monkeypatch.setattr(doa, "fetch_detail", Mock(side_effect=RuntimeError("timeout")))
    existing = {"status": "old", "details": {"fields": {"销售订单": "SO-1"}}}
    stats = {"detail_errors": 0, "skipped_details": 0}
    result = doa.refreshed_details(object(), {"status": "new"}, existing, stats)
    assert result["details"] == existing["details"]
    assert result["_detail_pending"]
    assert stats["detail_errors"] == 1


def test_shared_order_normalization_preserves_alphanumeric_padding():
    assert runtime.order_key("000123") == "123"
    assert runtime.order_key("000ABC") == "000ABC"


@pytest.mark.parametrize("orders", [False, True])
def test_bom_backfill_bounds_all_branches_and_skips_unchanged(orders):
    from scripts.maintenance import backfill_order_bom_planned_start as bom
    source = Mock()
    source.count_documents.return_value = 1
    source.find.return_value = [{"data": {"AUFNR": "1", "GSTRS": "20260901"}}]
    target = Mock()
    target.count_documents.return_value = 11
    target.find.return_value = [
        {"_id": str(i), "GSTRS": "20260901", "AUFNR_1": "unknown" if orders else ""}
        for i in range(10)
    ] + [{"_id": "unchanged", "AUFNR_1": "1", "GSTRS": "2026-09-01", "GSTRS_DATE": "2026-09-01"}]
    sizes = []
    def write(operations, **_kwargs):
        sizes.append(len(operations))
        return SimpleNamespace(matched_count=len(operations), modified_count=len(operations))
    target.bulk_write.side_effect = write
    result = bom.backfill({"bom": target, "sales": source}, bom_collection_name="bom",
                          sales_collection_name="sales", batch_size=3, preview_limit=0, apply=True)
    assert sizes == [3, 3, 3, 1]
    assert result["write_attempted"] == 10 and result["unchanged"] == 1
