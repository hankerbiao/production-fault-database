from types import SimpleNamespace
from unittest.mock import ANY
from datetime import date

import pytest

from scripts.sync import 增量同步和清洗维修故障记录 as backfill


class Result:
    def __init__(self, modified_count=0):
        self.modified_count = modified_count


class Collection:
    def __init__(self, documents):
        self.documents = [dict(document) for document in documents]
        self.bulk_calls = 0

    def count_documents(self, query):
        return len(self._matching(query))

    def find(self, query, projection=None):
        documents = self._matching(query)
        if not projection:
            return documents
        result = []
        for document in documents:
            item = {field: document.get(field) for field in projection if "." not in field}
            if any(field.startswith("data.") for field in projection):
                item["data"] = dict(document.get("data") or {})
            result.append(item)
        return result

    def bulk_write(self, operations, ordered=False):
        self.bulk_calls += 1
        changed = 0
        for operation in operations:
            filters = operation._filter.get("$and", [operation._filter])
            identity = next(item for item in filters if "_id" in item)
            target = next(document for document in self.documents if document["_id"] == identity["_id"])
            if any(not backfill.text(target.get(field)) for field in operation._doc["$set"]):
                target.update(operation._doc["$set"])
                changed += 1
        return Result(changed)

    def _matching(self, query):
        if not query:
            return [dict(document) for document in self.documents]
        if "$or" not in query:
            raise AssertionError(f"Unexpected query: {query}")
        missing_fields = set()

        def collect_fields(condition):
            if "$or" in condition:
                for item in condition["$or"]:
                    collect_fields(item)
            else:
                missing_fields.update(condition)

        collect_fields(query)
        return [dict(document) for document in self.documents if any(not backfill.text(document.get(field)) for field in missing_fields)]


class AuditCollection:
    def __init__(self):
        self.documents = {}

    def bulk_write(self, operations, ordered=False):
        for operation in operations:
            document = self.documents.setdefault(operation._filter["_id"], {"_id": operation._filter["_id"]})
            document.update(operation._doc["$set"])
        return Result(len(operations))


class DB:
    def __init__(self, stations, repairs, orders=()):
        self.collections = {
            "stations": Collection(stations),
            "repairs": Collection(repairs),
            "orders": Collection(orders),
            "audit": AuditCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def args(apply=False):
    return SimpleNamespace(
        station_collection="stations",
        repair_collection="repairs",
        batch_size=2,
        preview_limit=10,
        no_progress=True,
        apply=apply,
        repair_pcode_fallback=False,
        sap_pcode_fallback=False,
        sales_order_fallback=False,
        planned_start_fallback=False,
        sales_order_collection="orders",
        one_click=False,
        allow_partial_sap=False,
        audit_collection="",
        sync_repair_hana=False,
        sync_end_date=date(2026, 9, 3),
        sync_lookback_days=7,
        sap_batch_size=2,
        sap_retries=0,
        sap_retry_delay=0,
        sap_timeout=1,
    )


def test_dry_run_builds_unique_collection_a_and_does_not_write():
    db = DB(
        [
            {"_id": "s1", "PCODE": "SN-1", "AUFNR": "P-1", "KDAUF": "S-1"},
            {"_id": "s2", "PCODE": "SN-1", "AUFNR": "P-1", "KDAUF": "S-1"},
            {"_id": "s3", "PCODE": "SN-2", "AUFNR": "P-2", "KDAUF": "S-2"},
            {"_id": "s4", "PCODE": "SN-2", "AUFNR": "P-3", "KDAUF": "S-3"},
            {"_id": "s5", "PCODE": "SN-3", "AUFNR": "", "KDAUF": "S-3"},
        ],
        [
            {"_id": "r1", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""},
            {"_id": "r2", "PCODE": "SN-2", "AUFNR": "", "VBELN": ""},
            {"_id": "r3", "PCODE": "SN-3", "AUFNR": "", "VBELN": ""},
            {"_id": "r4", "PCODE": "SN-4", "AUFNR": "", "VBELN": ""},
            {"_id": "r5", "PCODE": "SN-1", "AUFNR": "P-old", "VBELN": "already"},
        ],
    )

    result = backfill.run_workflow(db, args())

    assert result["collection_a_size"] == 1
    assert result["station_ambiguous_sns"] == 1
    assert result["repair_candidates"] == 5
    assert result["would_update"] == 1
    assert result["updated"] == 0
    assert result["preview"] == [{
        "id": "r1", "PCODE": "SN-1",
        "source": "station",
        "AUFNR": {"before": "", "after": "P-1"},
        "VBELN": {"before": "", "after": "S-1"},
        "GSTRS": {"before": "", "after": ""},
    }]
    assert db["repairs"].bulk_calls == 0


def test_apply_does_not_overwrite_a_conflicting_existing_production_order():
    db = DB(
        [{"_id": "s1", "PCODE": "SN-1", "AUFNR": "P-1", "KDAUF": "S-1"}],
        [
            {"_id": "r1", "PCODE": "SN-1", "AUFNR": "P-old", "VBELN": "  "},
            {"_id": "r2", "PCODE": "SN-1", "AUFNR": "P-2", "VBELN": "S-2"},
        ],
    )

    result = backfill.run_workflow(db, args(apply=True))

    assert result["would_update"] == 0
    assert result["updated"] == 0
    assert result["repair_existing_aufnr_differs"] == 1
    assert result["station_skipped_existing_aufnr_conflict"] == 1
    assert db["repairs"].documents == [
        {"_id": "r1", "PCODE": "SN-1", "AUFNR": "P-old", "VBELN": "  "},
        {"_id": "r2", "PCODE": "SN-1", "AUFNR": "P-2", "VBELN": "S-2"},
    ]


def test_repair_pcode_fallback_backfills_only_unique_complete_repair_pairs():
    db = DB(
        [],
        [
            {"_id": "known", "PCODE": "SN-1", "AUFNR": "P-1", "VBELN": "S-1"},
            {"_id": "missing", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""},
            {"_id": "conflict-1", "PCODE": "SN-2", "AUFNR": "P-2", "VBELN": "S-2"},
            {"_id": "conflict-2", "PCODE": "SN-2", "AUFNR": "P-3", "VBELN": "S-3"},
            {"_id": "conflict-missing", "PCODE": "SN-2", "AUFNR": "", "VBELN": ""},
        ],
    )
    workflow_args = args(apply=True)
    workflow_args.repair_pcode_fallback = True

    result = backfill.run_workflow(db, workflow_args)

    assert result["repair_pcode_fallback"] is True
    assert result["repair_collection_b_size"] == 1
    assert result["repair_source_ambiguous_sns"] == 1
    assert result["station_matches"] == 0
    assert result["repair_pcode_matches"] == 1
    assert result["updated"] == 1
    assert db["repairs"].documents[1] == {"_id": "missing", "PCODE": "SN-1", "AUFNR": "P-1", "VBELN": "S-1"}
    assert db["repairs"].documents[4] == {"_id": "conflict-missing", "PCODE": "SN-2", "AUFNR": "", "VBELN": ""}


def test_sap_pcode_fallback_fills_only_an_empty_production_order():
    db = DB([], [{"_id": "missing", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}])
    workflow_args = args(apply=True)
    workflow_args.sap_pcode_fallback = True

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"MSGTY": "S", "DATA": [{"PCODE": "SN-1", "AUFNR": "123"}]}

    class Client:
        def __init__(self, **_kwargs):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    result = backfill.run_workflow(db, workflow_args, sap_client_factory=Client)

    assert result["sap_pcode_candidates"] == 1
    assert result["sap_kk_found"] == 1
    assert result["sap_pcode_matches"] == 1
    assert result["updated"] == 1
    assert db["repairs"].documents == [{"_id": "missing", "PCODE": "SN-1", "AUFNR": "123", "VBELN": ""}]


def test_sales_order_fallback_fills_sales_order_for_an_existing_production_order():
    db = DB(
        [],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "000123", "VBELN": ""}],
        [{"_id": "order", "aufnr": "123", "data": {"AUFNR": "123", "VBELN": "S-123"}}],
    )
    workflow_args = args(apply=True)
    workflow_args.sales_order_fallback = True

    result = backfill.run_workflow(db, workflow_args)

    assert result["sales_detail_pair_count"] == 1
    assert result["sales_order_detail_matches"] == 1
    assert result["updated"] == 1
    assert db["repairs"].documents == [
        {"_id": "repair", "PCODE": "SN-1", "AUFNR": "000123", "VBELN": "S-123"},
    ]


def test_planned_start_fallback_fills_empty_gstrs_from_sales_order_detail():
    db = DB(
        [],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "000123", "VBELN": "S-123", "GSTRS": ""}],
        [{"_id": "order", "aufnr": "123", "data": {"AUFNR": "123", "VBELN": "S-123", "GSTRS": "2026-09-01"}}],
    )
    workflow_args = args(apply=True)
    workflow_args.planned_start_fallback = True

    result = backfill.run_workflow(db, workflow_args)

    assert result["planned_start_unique_production_orders"] == 1
    assert result["planned_start_candidates"] == 1
    assert result["planned_start_matches"] == 1
    assert result["updated"] == 1
    assert db["repairs"].documents == [
        {"_id": "repair", "PCODE": "SN-1", "AUFNR": "000123", "VBELN": "S-123", "GSTRS": "2026-09-01"},
    ]


def test_planned_start_fallback_skips_ambiguous_sales_order_dates():
    db = DB(
        [],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "123", "VBELN": "S-123", "GSTRS": ""}],
        [
            {"_id": "order-1", "aufnr": "123", "data": {"AUFNR": "123", "VBELN": "S-123", "GSTRS": "2026-09-01"}},
            {"_id": "order-2", "aufnr": "123", "data": {"AUFNR": "123", "VBELN": "S-123", "GSTRS": "2026-09-02"}},
        ],
    )
    workflow_args = args(apply=True)
    workflow_args.planned_start_fallback = True

    result = backfill.run_workflow(db, workflow_args)

    assert result["planned_start_ambiguous_production_orders"] == 1
    assert result["planned_start_matches"] == 0
    assert result["updated"] == 0
    assert db["repairs"].documents[0]["GSTRS"] == ""


def test_sales_order_fallback_combines_sap_production_order_with_sales_detail():
    db = DB(
        [],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
        [{"_id": "order", "aufnr": "123", "data": {"AUFNR": "123", "VBELN": "S-123"}}],
    )
    workflow_args = args(apply=True)
    workflow_args.sap_pcode_fallback = True
    workflow_args.sales_order_fallback = True

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"MSGTY": "S", "DATA": [{"PCODE": "SN-1", "AUFNR": "000123"}]}

    class Client:
        def __init__(self, **_kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            return Response()

    result = backfill.run_workflow(db, workflow_args, sap_client_factory=Client)

    assert result["sap_sales_order_detail_matches"] == 1
    assert result["sap_pcode_matches"] == 0
    assert result["updated"] == 1
    assert db["repairs"].documents == [
        {"_id": "repair", "PCODE": "SN-1", "AUFNR": "000123", "VBELN": "S-123"},
    ]


def test_sales_order_fallback_skips_ambiguous_sales_orders():
    db = DB(
        [],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "123", "VBELN": ""}],
        [
            {"_id": "order-1", "aufnr": "123", "data": {"AUFNR": "123", "VBELN": "S-1"}},
            {"_id": "order-2", "aufnr": "123", "data": {"AUFNR": "123", "VBELN": "S-2"}},
        ],
    )
    workflow_args = args(apply=True)
    workflow_args.sales_order_fallback = True

    result = backfill.run_workflow(db, workflow_args)

    assert result["sales_detail_ambiguous_production_orders"] == 1
    assert result["would_update"] == 0
    assert result["updated"] == 0
    assert db["repairs"].documents == [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "123", "VBELN": ""}]


def test_one_click_enables_every_backfill_source():
    db = DB(
        [{"_id": "station", "PCODE": "SN-1", "AUFNR": "P-1", "KDAUF": "S-1"}],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
    )
    workflow_args = args()
    workflow_args.one_click = True

    result = backfill.run_workflow(db, workflow_args)

    assert result["one_click"] is True
    assert result["repair_pcode_fallback"] is True
    assert result["sap_pcode_fallback"] is True
    assert result["sales_order_fallback"] is True
    assert result["station_matches"] == 1


def test_main_parser_defaults_to_hana_sync_and_complete_backfill():
    parsed = backfill.build_parser().parse_args([])

    assert parsed.sync_repair_hana is True
    assert parsed.one_click is True

    basic = backfill.build_parser().parse_args(["--skip-hana-sync", "--basic-backfill"])
    assert basic.sync_repair_hana is False
    assert basic.one_click is False


def test_apply_stops_before_writing_when_sap_queries_fail():
    db = DB([], [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}])
    workflow_args = args(apply=True)
    workflow_args.sap_pcode_fallback = True

    class Client:
        def __init__(self, **_kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, **_kwargs):
            raise backfill.httpx.ConnectError("SAP unavailable")

    with pytest.raises(RuntimeError, match="已阻断写入"):
        backfill.run_workflow(db, workflow_args, sap_client_factory=Client)

    assert db["repairs"].documents == [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}]


def test_apply_persists_an_audit_record_for_each_repair_write():
    db = DB(
        [{"_id": "station", "PCODE": "SN-1", "AUFNR": "P-1", "KDAUF": "S-1"}],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
    )
    workflow_args = args(apply=True)
    workflow_args.audit_collection = "audit"

    result = backfill.run_workflow(db, workflow_args, run_id="run-1")

    assert result["audit_records"] == 1
    assert db["audit"].documents["run-1:repair"] == {
        "_id": "run-1:repair",
        "run_id": "run-1",
        "repair_id": "repair",
        "PCODE": "SN-1",
        "source": "station",
        "before": {"AUFNR": "", "VBELN": "", "GSTRS": ""},
        "after": {"AUFNR": "P-1", "VBELN": "S-1", "GSTRS": ""},
        "fields": {"AUFNR": "P-1", "VBELN": "S-1"},
        "write_status": "attempted",
        "updated_at": ANY,
    }


def test_hana_sync_runs_before_backfill_and_commits_after_success(monkeypatch):
    db = DB(
        [{"_id": "station", "PCODE": "SN-1", "AUFNR": "P-1", "KDAUF": "S-1"}],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
    )
    workflow_args = args(apply=True)
    workflow_args.sync_repair_hana = True
    calls = []

    def fake_sync(_db, collection, checkpoint_collection, mode, start_date, end_date, lookback_days, batch_size, dry_run, run_id, defer_finalize):
        calls.append(("sync", collection, checkpoint_collection, mode, start_date, end_date, lookback_days, batch_size, dry_run, run_id, defer_finalize))
        return {"success": True, "mode": "incremental", "watermark": {"ZDATE": "20260903", "ZTIME": "010000"}}

    def fake_finalize(_db, collection, checkpoint_collection, sync_result, run_id):
        calls.append(("commit", collection, checkpoint_collection, sync_result, run_id))
        return {"deleted_out_of_scope": 0}

    monkeypatch.setattr(backfill.sync_sales_orders, "sync_repair", fake_sync)
    monkeypatch.setattr(backfill.sync_sales_orders, "finalize_repair_run", fake_finalize)

    result = backfill.run_workflow(db, workflow_args, run_id="run-1")

    assert calls == [
        ("sync", "repairs", "sync_checkpoints", "incremental", None, date(2026, 9, 3), 7, 2, False, "run-1", True),
        ("commit", "repairs", "sync_checkpoints", result["hana_sync"], "run-1"),
    ]
    assert result["updated"] == 1
    assert result["hana_sync_commit"] == {"deleted_out_of_scope": 0}


def test_hana_sync_failure_stops_order_backfill(monkeypatch):
    db = DB(
        [{"_id": "station", "PCODE": "SN-1", "AUFNR": "P-1", "KDAUF": "S-1"}],
        [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
    )
    workflow_args = args(apply=True)
    workflow_args.sync_repair_hana = True
    monkeypatch.setattr(backfill.sync_sales_orders, "sync_repair", lambda *_args, **_kwargs: {"success": False, "error": "HANA unavailable"})

    with pytest.raises(RuntimeError, match="HANA 维修故障记录同步失败"):
        backfill.run_workflow(db, workflow_args)

    assert db["repairs"].documents == [{"_id": "repair", "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}]
