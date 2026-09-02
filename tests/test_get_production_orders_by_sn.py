from datetime import date

import pytest

from scripts.maintenance import repair_cleanup as lookup


def test_headers_follow_documented_signature_rule():
    headers = lookup.request_headers(date(2026, 3, 9))
    assert headers == {
        "Content-Type": "application/json",
        "method": "ZSIMS_CL_INBOUND_SN_INFO",
        "sign": "FC58625D531DFF2609456F2338FAE76E",
        "time": "20260309",
    }


def test_configure_logging_writes_process_records_to_file(tmp_path):
    log_file = tmp_path / "production-orders.log"
    lookup.configure_logging("INFO", str(log_file))
    try:
        lookup.LOGGER.info("日志测试: batch=%d", 1)
        for handler in lookup.LOGGER.handlers:
            handler.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "INFO 日志测试: batch=1" in content
    finally:
        lookup.configure_logging("CRITICAL")


def test_read_serial_numbers_deduplicates_cli_and_csv_input(tmp_path):
    input_file = tmp_path / "serials.csv"
    input_file.write_text("SN,备注\n6102139607654349,first\n6102139607654349,duplicate\n6102139607654350,next\n", encoding="utf-8")

    assert lookup.read_serial_numbers(str(input_file), None) == ["6102139607654349", "6102139607654350"]
    assert lookup.read_serial_numbers(None, [" A ", "", "A", "B"]) == ["A", "B"]


def test_repair_filter_can_include_historical_source_views():
    args = lookup.build_parser().parse_args(["--repair", "--all-source-views"])
    assert lookup.repair_filter(args) == {}


def test_validate_and_map_results_preserves_not_found_status():
    assert lookup.validate_response({"MSGTY": "E", "MSGTX": "No Data"}) == []
    records = lookup.validate_response({"MSGTY": "S", "DATA": [{"PCODE": "SN-1", "AUFNR": "30211673", "MATNR": "M-1", "MAKTX": "machine"}]})
    rows = lookup.rows_for_serial_numbers(["SN-1", "SN-2"], records)

    assert rows[0]["production_order"] == "30211673"
    assert rows[0]["status"] == "found"
    assert rows[1] == {"sn": "SN-2", "production_order": "", "status": "not_found", "error": ""}
    with pytest.raises(lookup.QueryError, match="SAP 返回错误"):
        lookup.validate_response({"MSGTY": "E", "MSGTX": "not permitted"})


def test_query_batch_posts_documented_payload_and_headers():
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"MSGTY": "S", "DATA": [{"PCODE": "SN-1", "AUFNR": "ORDER-1"}]}

    class Client:
        call = None

        def post(self, *args, **kwargs):
            self.call = (args, kwargs)
            return Response()

    client = Client()
    rows = lookup.query_batch(client, "https://sap.example.test", ["SN-1"], retries=0, retry_delay=0)

    assert client.call[0] == ("https://sap.example.test",)
    assert client.call[1]["json"] == {"SN_LIST": ["SN-1"]}
    assert client.call[1]["headers"]["method"] == lookup.METHOD
    assert rows[0]["production_order"] == "ORDER-1"


def test_run_marks_only_failed_batch_when_a_request_errors(monkeypatch):
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def query_batch(_client, _url, serial_numbers, _retries, _delay):
        calls.append(serial_numbers)
        if serial_numbers == ["SN-3"]:
            raise lookup.QueryError("network timeout")
        return [{"sn": sn, "production_order": "ORDER", "status": "found", "error": ""} for sn in serial_numbers]

    monkeypatch.setattr(lookup, "query_batch", query_batch)
    args = lookup.build_parser().parse_args(["--sn", "SN-1", "SN-2", "SN-3", "--batch-size", "2"])
    rows, success = lookup.run(args, client_factory=lambda **_kwargs: Client())

    assert calls == [["SN-1", "SN-2"], ["SN-3"]]
    assert success is False
    assert [row["status"] for row in rows] == ["found", "found", "failed"]


class RepairCollection:
    def __init__(self, docs):
        self.docs = [dict(doc) for doc in docs]
        self.bulk_calls = 0
        self.delete_calls = 0

    def find(self, query, projection=None):
        def matches(doc):
            if query.get("_source_view") and doc.get("_source_view") != query["_source_view"]:
                return False
            pcode_filter = query.get("PCODE")
            if isinstance(pcode_filter, dict) and "$in" in pcode_filter and doc.get("PCODE") not in pcode_filter["$in"]:
                return False
            return True

        if not projection:
            return [dict(doc) for doc in self.docs if matches(doc)]
        result = []
        for doc in self.docs:
            if not matches(doc):
                continue
            item = {key: doc.get(key) for key in projection if "." not in key}
            if "data.AUFNR" in projection or "data.VBELN" in projection:
                item["data"] = dict(doc.get("data") or {})
            result.append(item)
        return result

    def bulk_write(self, operations, ordered=False):
        self.bulk_calls += 1
        changed = 0
        for operation in operations:
            target = next((doc for doc in self.docs if doc.get("_id") == operation._filter.get("_id")), None)
            if target is None:
                continue
            target.update(operation._doc["$set"])
            changed += 1
        return type("Result", (), {"modified_count": changed})()

    def delete_many(self, query):
        self.delete_calls += 1
        candidates = query["$or"]
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if not any(all(doc.get(key) == value for key, value in match.items()) for match in candidates)]
        return type("Result", (), {"deleted_count": before - len(self.docs)})()


class RepairDB:
    def __init__(self, repairs, orders, stations=None):
        station_collection = RepairCollection(stations or [])
        self.collections = {
            "repairs": RepairCollection(repairs),
            "orders": RepairCollection(orders),
            "stations": station_collection,
            "station_records_sap": station_collection,
        }

    def __getitem__(self, name):
        return self.collections[name]


class RepairClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_repair_workflow_queries_kk_then_sg_and_keeps_dry_run_read_only(monkeypatch):
    db = RepairDB(
        [
            {"_id": "r1", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "", "VBELN": ""},
            {"_id": "r2", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-2", "AUFNR": "0002", "VBELN": ""},
            {"_id": "r3", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-3", "AUFNR": "", "VBELN": ""},
        ],
        [
            {"source": "KK", "aufnr": "1", "data": {"AUFNR": "000000000001", "VBELN": "SO-1"}},
            {"source": "SG", "aufnr": "2", "data": {"AUFNR": "2", "VBELN": "SO-2"}},
        ],
    )
    calls = []

    def fake_query(_client, url, sns, _retries, _delay):
        calls.append((url, sns))
        if "10.8.100.11" in url:
            return [{"sn": sn, "production_order": "1", "status": "found"} for sn in sns if sn == "SN-1"] + [{"sn": sn, "production_order": "", "status": "not_found"} for sn in sns if sn == "SN-3"]
        return [{"sn": "SN-3", "production_order": "", "status": "not_found"}]

    monkeypatch.setattr(lookup, "query_batch", fake_query)
    args = lookup.build_parser().parse_args(["--repair", "--dry-run", "--batch-size", "2"])
    args.repair_collection, args.order_collection = "repairs", "orders"
    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert calls == [(lookup.DEFAULT_URLS["KK"], ["SN-1"]), (lookup.DEFAULT_URLS["KK"], ["SN-3"]), (lookup.DEFAULT_URLS["SG"], ["SN-3"])]
    assert result["kk_found"] == 1 and result["sg_found"] == 0
    assert result["update_candidates"] == 2 and result["skipped_sap_not_found"] == 1
    assert db["repairs"].bulk_calls == 0 and db["repairs"].delete_calls == 0


def test_repair_apply_fills_missing_fields_and_deletes_unknown_order(monkeypatch):
    db = RepairDB(
        [
            {"_id": "known", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "", "VBELN": ""},
            {"_id": "unknown", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-9", "AUFNR": "", "VBELN": ""},
        ],
        [{"source": "KK", "aufnr": "1", "data": {"AUFNR": "1", "VBELN": "SO-1"}}],
    )

    def fake_query(_client, _url, sns, _retries, _delay):
        return [{"sn": sn, "production_order": ("1" if sn == "SN-1" else "9"), "status": "found"} for sn in sns]

    monkeypatch.setattr(lookup, "query_batch", fake_query)
    args = lookup.build_parser().parse_args(["--repair", "--apply", "--confirm", lookup.REPAIR_CONFIRMATION])
    args.repair_collection, args.order_collection = "repairs", "orders"
    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert result["updates"] == 1 and result["deleted"] == 1
    assert db["repairs"].docs == [{"_id": "known", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "1", "VBELN": "SO-1"}]


def test_order_present_without_sales_order_is_retained(monkeypatch):
    db = RepairDB(
        [{"_id": "incomplete", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
        [{"source": "KK", "aufnr": "1", "data": {"AUFNR": "1", "VBELN": ""}}],
    )
    monkeypatch.setattr(lookup, "query_batch", lambda *_args: [{"sn": "SN-1", "production_order": "1", "status": "found"}])
    args = lookup.build_parser().parse_args(["--repair", "--apply", "--confirm", lookup.REPAIR_CONFIRMATION])
    args.repair_collection, args.order_collection = "repairs", "orders"

    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert result["skipped_board_missing_sales"] == 1
    assert result["deleted"] == 0 and db["repairs"].delete_calls == 0


def test_missing_sales_only_limits_candidates_to_empty_vbeln(monkeypatch):
    db = RepairDB(
        [
            {"_id": "invalid-sales", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-INVALID", "AUFNR": "", "VBELN": "SO-INVALID"},
            {"_id": "missing-sales", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "1", "VBELN": ""},
        ],
        [{"source": "KK", "aufnr": "1", "data": {"AUFNR": "1", "VBELN": "SO-1"}}],
    )
    monkeypatch.setattr(lookup, "query_batch", lambda *_args: [])
    args = lookup.build_parser().parse_args(["--repair", "--dry-run", "--missing-sales-only", "--limit", "1"])
    args.repair_collection, args.order_collection = "repairs", "orders"

    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert result["scanned"] == 1
    assert result["missing_sales_only"] is True
    assert result["update_candidates"] == 1
    assert result["delete_candidates"] == 0


def test_repair_workflow_fills_orders_from_unique_station_record_without_sap(monkeypatch):
    db = RepairDB(
        [{"_id": "r1", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
        [],
        [{
            "_id": "s1", "_source_view": lookup.STATION_SOURCE_VIEW, "PCODE": "SN-1",
            "AUFNR": "000000000001", "KDAUF": "SO-1",
        }],
    )
    monkeypatch.setattr(lookup, "query_batch", lambda *_args: pytest.fail("不应请求 SAP"))
    args = lookup.build_parser().parse_args(["--repair", "--apply", "--confirm", lookup.REPAIR_CONFIRMATION])
    args.repair_collection, args.order_collection, args.station_collection = "repairs", "orders", "stations"

    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert result["updates"] == 1
    assert result["filled_production_from_station"] == 1
    assert result["filled_sales_from_station"] == 1
    assert result["station_matched_sns"] == 1
    assert db["repairs"].docs[0]["AUFNR"] == "000000000001"
    assert db["repairs"].docs[0]["VBELN"] == "SO-1"


def test_ambiguous_station_orders_fall_back_to_sap(monkeypatch):
    db = RepairDB(
        [{"_id": "r1", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "", "VBELN": ""}],
        [{"source": "KK", "aufnr": "3", "data": {"AUFNR": "3", "VBELN": "SO-3"}}],
        [
            {"_id": "s1", "_source_view": lookup.STATION_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "1", "KDAUF": ""},
            {"_id": "s2", "_source_view": lookup.STATION_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "2", "KDAUF": ""},
        ],
    )
    calls = []

    def fake_query(_client, _url, sns, _retries, _delay):
        calls.append(sns)
        return [{"sn": "SN-1", "production_order": "3", "status": "found"}]

    monkeypatch.setattr(lookup, "query_batch", fake_query)
    args = lookup.build_parser().parse_args(["--repair", "--dry-run"])
    args.repair_collection, args.order_collection, args.station_collection = "repairs", "orders", "stations"

    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert calls == [["SN-1"]]
    assert result["station_ambiguous_production_sns"] == 1
    assert result["filled_production_from_station"] == 0
    assert result["update_candidates"] == 1


def test_repair_workflow_fills_planned_start_from_unique_sales_order_date(monkeypatch):
    db = RepairDB(
        [{"_id": "r1", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "0001", "VBELN": "SO-1", "GSTRS": ""}],
        [{"source": "KK", "aufnr": "1", "data": {"AUFNR": "1", "VBELN": "SO-1", "GSTRS": "2026-01-03"}}],
    )
    monkeypatch.setattr(lookup, "query_batch", lambda *_args: pytest.fail("完整生产订单不应请求 SAP"))
    args = lookup.build_parser().parse_args(["--repair", "--apply", "--confirm", lookup.REPAIR_CONFIRMATION])
    args.repair_collection, args.order_collection = "repairs", "orders"

    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert result["planned_start_candidates"] == 1
    assert result["updates"] == 1
    assert db["repairs"].docs[0]["GSTRS"] == "2026-01-03"


def test_repair_workflow_skips_ambiguous_planned_start_and_deletes_complete_orphan(monkeypatch):
    db = RepairDB(
        [
            {"_id": "ambiguous", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "1", "VBELN": "SO-1", "GSTRS": ""},
            {"_id": "orphan", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-2", "AUFNR": "9", "VBELN": "SO-9", "GSTRS": ""},
        ],
        [
            {"source": "KK", "aufnr": "1", "data": {"AUFNR": "1", "VBELN": "SO-1", "GSTRS": "2026-01-03"}},
            {"source": "SG", "aufnr": "1", "data": {"AUFNR": "1", "VBELN": "SO-1", "GSTRS": "2026-01-04"}},
        ],
    )
    monkeypatch.setattr(lookup, "query_batch", lambda *_args: pytest.fail("完整生产订单不应请求 SAP"))
    args = lookup.build_parser().parse_args(["--repair", "--apply", "--confirm", lookup.REPAIR_CONFIRMATION])
    args.repair_collection, args.order_collection = "repairs", "orders"

    result = lookup.repair_workflow(db, args, client_factory=lambda **_kwargs: RepairClient())

    assert result["ambiguous_planned_start_orders"] == 1
    assert result["skipped_ambiguous_planned_start"] == 1
    assert result["deleted"] == 1
    assert db["repairs"].docs == [{"_id": "ambiguous", "_source_view": lookup.REPAIR_SOURCE_VIEW, "PCODE": "SN-1", "AUFNR": "1", "VBELN": "SO-1", "GSTRS": ""}]
