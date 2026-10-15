from contextlib import contextmanager, nullcontext
from datetime import date
import re
from types import SimpleNamespace

import pytest

from scripts.sync import sync_sales_orders as sync
from test_sync_sales_orders import FakeCollection, FakeDB


class SnapshotCollection(FakeCollection):
    def _match(self, doc, query):
        for key, expected in query.items():
            if key == "$and":
                if not all(self._match(doc, part) for part in expected):
                    return False
                continue
            if key == "$or":
                if not any(self._match(doc, part) for part in expected):
                    return False
                continue
            actual = doc.get(key)
            if isinstance(expected, dict):
                if "$exists" in expected and (key in doc) != expected["$exists"]:
                    return False
                if "$regex" in expected and (not isinstance(actual, str) or not re.search(expected["$regex"], actual)):
                    return False
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
                if "$gte" in expected and (actual is None or actual < expected["$gte"]):
                    return False
                if "$lte" in expected and (actual is None or actual > expected["$lte"]):
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query=None, projection=None, **_kwargs):
        return [dict(doc) for doc in self.docs if self._match(doc, query or {})]

    def bulk_write(self, operations, ordered=False):
        inserted = modified = 0
        for op in operations:
            doc = next((d for d in self.docs if self._match(d, op._filter)), None)
            if doc is None:
                if not op._upsert:
                    continue
                doc = {k: v for k, v in op._filter.items() if not isinstance(v, dict)}
                doc.update(op._doc.get("$setOnInsert", {}))
                self.docs.append(doc)
                inserted += 1
            else:
                modified += 1
            doc.update(op._doc.get("$set", {}))
            for key in op._doc.get("$unset", {}):
                doc.pop(key, None)
        return SimpleNamespace(upserted_count=inserted, modified_count=modified)


class SnapshotDB(FakeDB):
    def __getitem__(self, name):
        return self.collections.setdefault(name, SnapshotCollection())


def repair(**overrides):
    row = {field: "" for field in sync.REPAIR_COLUMNS}
    row.update({
        "MANDT": "600", "PCODE": "PC-1", "ZWXDT": "20260922104832",
        "TESTID": "20260922104832", "ZDATE_WX": "20260922",
        "ZDATE": "20260922", "ZTIME": "163129", "VBELN": "SO-1",
        "AUFNR": "PO-1", "ZGZMS": "repair",
    })
    row.update(overrides)
    return row


def seed_db(monkeypatch):
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    db = SnapshotDB()
    db["orders"].docs = [{"_id": "order", "data": {"VBELN": "SO-1"}}]
    db["checkpoints"].docs = [{"_id": "repair_records", "watermark": {"ZDATE": "20261013", "ZTIME": "235959"}}]
    return db


def source(monkeypatch, rows, *, fail_after_first=False):
    remaining = list(rows)
    calls = []

    class Cursor:
        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchmany(self, size):
            if fail_after_first and calls and len(remaining) < len(rows):
                raise RuntimeError("HANA fetch interrupted")
            batch = remaining[:size]
            del remaining[:size]
            return [tuple(row.get(field) for field in sync.REPAIR_COLUMNS) for row in batch]

        def close(self):
            pass

    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr(sync, "hana_connection", connection)
    return calls


def run(db, run_id="run-1", **kwargs):
    return sync.sync_repair(
        db, "repairs", mode="incremental", start_date=None,
        end_date=date(2026, 10, 14), batch_size=1,
        dry_run=kwargs.pop("dry_run", False), run_id=run_id, **kwargs,
    )


@pytest.mark.parametrize("end,expected", [
    (date(2026, 10, 14), date(2026, 8, 1)),
    (date(2027, 1, 31), date(2026, 11, 1)),
    (date(2026, 2, 28), date(2026, 1, 1)),
])
def test_window_uses_three_calendar_months_and_hard_lower_bound(monkeypatch, end, expected):
    db = seed_db(monkeypatch)
    calls = source(monkeypatch, [])
    result = sync.sync_repair(db, "repairs", mode="incremental", start_date=None,
                             end_date=end, batch_size=1, dry_run=True, run_id="dry")
    assert result["range"] == {"start": expected.isoformat(), "end": end.isoformat()}
    assert calls[0][1] == (expected.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    assert '"ZDATE" >' not in calls[0][0]


def test_changed_read_time_updates_same_document_and_unchanged_run_skips_content(monkeypatch):
    db = seed_db(monkeypatch)
    source(monkeypatch, [repair()])
    first = run(db)
    assert first["inserted"] == 1
    original_id = db["repairs"].docs[0]["_id"]
    source(monkeypatch, [repair(ZDATE="20261001", ZTIME="221507", ZGZMS="updated")])
    second = run(db, "run-2")
    assert second["inserted"] == 0 and second["updated"] == 1
    assert len(db["repairs"].docs) == 1
    assert db["repairs"].docs[0]["_id"] == original_id
    assert db["repairs"].docs[0]["ZGZMS"] == "updated"
    synced_at = db["repairs"].docs[0]["_synced_at"]
    source(monkeypatch, [repair(ZDATE="20261001", ZTIME="221507", ZGZMS="updated")])
    third = run(db, "run-3")
    assert third["inserted"] == third["updated"] == 0 and third["unchanged"] == 1
    assert db["repairs"].docs[0]["_synced_at"] == synced_at
    assert db["repairs"].docs[0]["_scope_run_id"] == "run-3"
    assert db["checkpoints"].docs[0]["watermark"]["ZDATE"] == "20261013"


def test_legacy_duplicates_migrate_in_place_and_delete_old_copy(monkeypatch):
    db = seed_db(monkeypatch)
    old = repair()
    current = repair(ZDATE="20260926", ZTIME="221507")
    for row, identifier, synced in [(old, "old", "2026-09-22"), (current, "current", "2026-09-27")]:
        db["repairs"].docs.append({
            **row, "_id": identifier, "_source_key": sync.source_key(row, sync.REPAIR_KEY_FIELDS),
            "_source_view": "ZSGV_ZZT_WLJL", "_scope_run_id": "previous", "_synced_at": synced,
        })
    source(monkeypatch, [current])
    result = run(db)
    assert result["inserted"] == 0 and result["relinked"] == 1
    assert result["deleted_out_of_scope"] == 1
    assert len(db["repairs"].docs) == 1 and db["repairs"].docs[0]["_id"] == "current"
    assert {"old", "current", sync.source_key(old, sync.REPAIR_KEY_FIELDS)} <= set(db["repairs"].docs[0]["_legacy_ids"])
    assert "repairs_archive" not in db.collections


def test_replacement_details_at_same_event_time_remain_separate(monkeypatch):
    db = seed_db(monkeypatch)
    source(monkeypatch, [
        repair(ZMCOD1="part", ZMCOD2="replacement-1"),
        repair(ZMCOD1="part", ZMCOD2="replacement-2"),
    ])
    result = run(db)
    assert result["inserted"] == 2 and len(db["repairs"].docs) == 2


@pytest.mark.parametrize("broken", ["fetch", "conflict", "missing_identity", "wrong_date"])
def test_invalid_snapshot_does_not_write_or_cleanup(monkeypatch, broken):
    db = seed_db(monkeypatch)
    db["repairs"].docs = [{**repair(), "_id": "old", "_source_view": "ZSGV_ZZT_WLJL"}]
    before = [dict(d) for d in db["repairs"].docs]
    rows = [repair()]
    if broken == "conflict":
        rows.append(repair(ZGZMS="different event with same identity"))
    elif broken == "missing_identity":
        rows.append(repair(PCODE=""))
    elif broken == "wrong_date":
        rows.append(repair(ZDATE_WX="20260101"))
    source(monkeypatch, rows, fail_after_first=broken == "fetch")
    with pytest.raises((ValueError, RuntimeError)):
        run(db)
    assert db["repairs"].docs == before
    assert db["repairs"].indexes == []
    assert "repairs_archive" not in db.collections


def test_dry_run_predicts_changes_without_writing(monkeypatch):
    db = seed_db(monkeypatch)
    source(monkeypatch, [repair(), repair()])
    result = run(db, run_id="dry", dry_run=True)
    assert result["unique_rows"] == 1 and result["duplicate_source_rows"] == 1
    assert result["would_insert"] == 1
    assert db["repairs"].docs == [] and db["repairs"].indexes == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_snapshot_retains_repairs_when_sales_reference_is_empty(monkeypatch, dry_run):
    db = seed_db(monkeypatch)
    db["orders"].docs = []
    source(monkeypatch, [repair(), repair(PCODE="PC-2", VBELN="")])
    result = run(db, dry_run=dry_run)
    assert result["success"]
    assert result["sales_order_vbelns"] == 0
    assert result["unmatched_sales_orders_retained"] == 1
    assert result["empty_sales_orders_retained"] == 1
    assert len(db["repairs"].docs) == (0 if dry_run else 2)


def test_cleanup_uses_repair_date_and_keeps_outside_window(monkeypatch):
    db = seed_db(monkeypatch)
    db["repairs"].docs = [
        {**repair(PCODE="deleted"), "_id": "deleted", "_source_view": "ZSGV_ZZT_WLJL", "ZDATE": "20260101"},
        {**repair(ZDATE_WX="20260731", ZDATE="20261013"), "_id": "outside", "_source_view": "ZSGV_ZZT_WLJL"},
        {**repair(), "_id": "other", "_source_view": "other"},
    ]
    source(monkeypatch, [repair(PCODE="new")])
    result = run(db)
    assert result["deleted_out_of_scope"] == 1
    assert {"outside", "other"} <= {d["_id"] for d in db["repairs"].docs}


def test_empty_or_large_shrinking_snapshot_is_blocked_before_write(monkeypatch):
    db = seed_db(monkeypatch)
    db["repairs"].docs = [
        {**repair(PCODE=f"PC-{i}"), "_id": str(i), "_source_view": "ZSGV_ZZT_WLJL"}
        for i in range(250)
    ]
    for rows in [[], [repair(PCODE="new")]]:
        source(monkeypatch, rows)
        with pytest.raises(RuntimeError, match="清理"):
            run(db)
        assert len(db["repairs"].docs) == 250
        assert db["repairs"].indexes == []
    source(monkeypatch, [])
    result = run(db, allow_large_cleanup=True)
    assert result["deleted_out_of_scope"] == 250
    assert db["repairs"].docs == []
    assert "repairs_archive" not in db.collections


def test_deferred_cleanup_requires_validated_matching_run(monkeypatch):
    db = seed_db(monkeypatch)
    source(monkeypatch, [repair()])
    result = run(db, defer_finalize=True)
    with pytest.raises(ValueError):
        sync.finalize_repair_run(db, "repairs", result, "wrong-run")
    with pytest.raises(ValueError):
        sync.finalize_repair_run(db, "repairs", {**result, "snapshot_complete": False}, "run-1")


def test_source_refresh_preserves_backfilled_orders_and_invalidates_changed_production(monkeypatch):
    db = seed_db(monkeypatch)
    source(monkeypatch, [repair(AUFNR="", VBELN="")])
    run(db)
    db["repairs"].docs[0].update({"AUFNR": "filled", "VBELN": "filled-sales", "GSTRS": "2026-09-21"})
    source(monkeypatch, [repair(AUFNR="", VBELN="", ZGZMS="new description")])
    run(db, "run-2")
    doc = db["repairs"].docs[0]
    assert (doc["AUFNR"], doc["VBELN"], doc["GSTRS"]) == ("filled", "filled-sales", "2026-09-21")
    source(monkeypatch, [repair(AUFNR="new-production", VBELN="")])
    run(db, "run-3")
    assert (doc["AUFNR"], doc["VBELN"], doc["GSTRS"]) == ("new-production", "", "")


def test_workflow_only_backfills_current_snapshot_and_relevant_stations(monkeypatch):
    from scripts.sync import 增量同步和清洗维修故障记录 as backfill
    from test_backfill_repair_orders_from_station import args

    db = seed_db(monkeypatch)
    db["orders"].docs = [{"_id": "order", "aufnr": "PO-1", "data": {"AUFNR": "PO-1", "VBELN": "SO-1", "GSTRS": "2026-09-21"}}]
    db["stations"].docs = [
        {"_id": "station", "PCODE": "PC-1", "AUFNR": "PO-1", "KDAUF": "SO-1"},
        {"_id": "irrelevant", "PCODE": "OTHER", "AUFNR": "PO-2", "KDAUF": "SO-2"},
    ]
    db["repairs"].docs = [
        {**repair(ZDATE_WX="20260731", ZWXDT="20260731000000", AUFNR="", VBELN=""), "_id": "outside", "_source_view": sync.REPAIR_VIEW_ID},
    ]
    source(monkeypatch, [repair(AUFNR="", VBELN="")])
    workflow = args(apply=True)
    workflow.sync_repair_hana = True
    workflow.sync_end_date = date(2026, 10, 14)
    workflow.planned_start_fallback = True
    result = backfill.run_workflow(db, workflow, run_id="run-1")
    assert result["station_rows_scanned"] == 1
    assert result["repair_candidates"] == result["updated"] == 1
    current = next(doc for doc in db["repairs"].docs if doc["_id"] != "outside")
    assert (current["AUFNR"], current["VBELN"], current["GSTRS"]) == ("PO-1", "SO-1", "2026-09-21")
    outside = next(doc for doc in db["repairs"].docs if doc["_id"] == "outside")
    assert outside["AUFNR"] == outside["VBELN"] == ""


def test_backfill_failure_keeps_stale_rows_and_retry_completes(monkeypatch):
    from scripts.sync import 增量同步和清洗维修故障记录 as backfill
    from test_backfill_repair_orders_from_station import args

    db = seed_db(monkeypatch)
    db["repairs"].docs = [{**repair(PCODE="deleted"), "_id": "old", "_source_view": sync.REPAIR_VIEW_ID}]
    source(monkeypatch, [repair()])
    workflow = args(apply=True)
    workflow.sync_repair_hana = True
    workflow.sync_end_date = date(2026, 10, 14)
    original = backfill.backfill_repairs

    def fail(*_args, **_kwargs):
        raise RuntimeError("backfill failed")

    monkeypatch.setattr(backfill, "backfill_repairs", fail)
    with pytest.raises(RuntimeError, match="backfill failed"):
        backfill.run_workflow(db, workflow, run_id="failed")
    assert any(doc["_id"] == "old" for doc in db["repairs"].docs)
    assert "repairs_archive" not in db.collections
    monkeypatch.setattr(backfill, "backfill_repairs", original)
    source(monkeypatch, [repair()])
    result = backfill.run_workflow(db, workflow, run_id="retry")
    assert result["hana_sync_commit"]["deleted_out_of_scope"] == 1
    assert len(db["repairs"].docs) == 1


def test_delete_failure_propagates_and_preserves_stale_documents(monkeypatch):
    db = seed_db(monkeypatch)
    db["repairs"].docs = [{**repair(PCODE="deleted"), "_id": "old", "_source_view": sync.REPAIR_VIEW_ID}]
    source(monkeypatch, [repair()])
    result = run(db, defer_finalize=True)

    def fail(*_args, **_kwargs):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(db["repairs"], "delete_many", fail)
    with pytest.raises(RuntimeError, match="delete failed"):
        sync.finalize_repair_run(db, "repairs", result, "run-1")
    assert any(doc["_id"] == "old" for doc in db["repairs"].docs)


def test_dry_run_entry_does_not_acquire_mongo_lease(monkeypatch):
    from scripts.sync import 增量同步和清洗维修故障记录 as backfill

    workflow = backfill.build_parser().parse_args(["--dry-run", "--no-progress"])

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getitem__(self, _name):
            return SnapshotDB()

        def close(self):
            pass

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run attempted a MongoDB lease write")

    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setattr(backfill, "MongoClient", Client)
    monkeypatch.setattr(backfill, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(backfill, "load_dotenv", lambda: None)
    monkeypatch.setattr(backfill, "process_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(backfill, "resource_lock", forbidden)
    monkeypatch.setattr(backfill, "run_workflow", lambda *_args, **_kwargs: {"success": True})
    assert backfill.run(workflow)["success"]


def test_dry_run_backfills_new_source_rows_without_mutating_local_repairs(monkeypatch):
    from scripts.sync import 增量同步和清洗维修故障记录 as backfill
    db = seed_db(monkeypatch)
    db["station_records_sap"].docs = [
        {"_id": "station", "PCODE": "PC-NEW", "AUFNR": "PO-1", "KDAUF": "SO-1"},
    ]
    source(monkeypatch, [repair(PCODE="PC-NEW", AUFNR="", VBELN="")])
    args = backfill.build_parser().parse_args(["--dry-run", "--basic-backfill", "--no-progress"])
    args.repair_collection = "repairs"
    args.sales_order_collection = "orders"
    args.sync_end_date = date(2026, 10, 14)
    result = backfill.run_workflow(db, args)
    assert result["backfill_basis"] == "validated_source_snapshot"
    assert result["station_matches"] == 1
    assert result["would_update"] == 1
    assert db["repairs"].docs == []


def test_partial_target_write_is_detected_before_cleanup(monkeypatch):
    db = seed_db(monkeypatch)
    db["repairs"].docs = [{**repair(PCODE="deleted"), "_id": "old", "_source_view": sync.REPAIR_VIEW_ID}]
    source(monkeypatch, [repair()])
    result = run(db, defer_finalize=True)
    db["repairs"].docs = [doc for doc in db["repairs"].docs if doc["_id"] == "old"]
    with pytest.raises(RuntimeError, match="尚未完整写入"):
        sync.finalize_repair_run(db, "repairs", result, "run-1")
    assert db["repairs"].docs[0]["_id"] == "old"
    assert "repairs_archive" not in db.collections


def test_reported_five_versions_reconcile_to_three_distinct_repairs(monkeypatch):
    db = seed_db(monkeypatch)
    common = {"PCODE": "6102114804622477"}
    chain_old = repair(**common, ZGZMS="chain", ZWXDT="20260922104832")
    chain_new = {**chain_old, "ZDATE": "20260926", "ZTIME": "221507"}
    board_old = repair(**common, ZGZMS="board", ZWXDT="20260922163543",
                       ZMCOD1="2400292060003078", ZMCOD2="2400292030017585")
    board_new = {**board_old, "ZDATE": "20260926", "ZTIME": "221507"}
    third = repair(**common, ZGZMS="third", ZWXDT="20260926221926", ZDATE_WX="20260926",
                   ZMCOD1="2400293760010252", ZMCOD2="2400293760005350")
    for index, row in enumerate([chain_old, chain_new, board_old, board_new, third]):
        db["repairs"].docs.append({
            **row, "_id": str(index), "_source_key": sync.source_key(row, sync.REPAIR_KEY_FIELDS),
            "_source_view": sync.REPAIR_VIEW_ID, "_scope_run_id": "old", "_synced_at": str(index),
        })
    source(monkeypatch, [chain_new, board_new, third])
    result = run(db)
    assert result["inserted"] == 0 and result["deleted_out_of_scope"] == 2
    assert len(db["repairs"].docs) == 3
    assert {doc["ZGZMS"] for doc in db["repairs"].docs} == {"chain", "board", "third"}
    assert "repairs_archive" not in db.collections
    source(monkeypatch, [chain_new, board_new, third])
    repeated = run(db, "repeat")
    assert repeated["inserted"] == repeated["deleted_out_of_scope"] == 0
    assert repeated["unchanged"] == 3 and len(db["repairs"].docs) == 3
