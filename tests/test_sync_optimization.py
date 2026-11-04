import json

import pytest

from scripts.sync import sync_sales_orders as sync
from test_sync_sales_orders import FakeDB, FakeHTTPClient, FakeMongoClient, FakeResponse


@pytest.fixture
def sales_run(monkeypatch):
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("MONGODB_HOSTS", "localhost:27017")
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    monkeypatch.setenv("SYNC_CHECKPOINT_COLLECTION", "checkpoints")
    monkeypatch.setenv("SYNC_RUN_COLLECTION", "runs")
    monkeypatch.setenv("FULL_WINDOW_DAYS", "7")
    monkeypatch.setattr(sync, "SOURCES", {"SG": "http://sg"})
    client = FakeMongoClient()
    monkeypatch.setattr(sync, "MongoClient", lambda *a, **k: client)

    def run(responses, *extra):
        http = FakeHTTPClient({"http://sg": responses})
        monkeypatch.setattr(sync.httpx, "Client", lambda **k: http)
        args = sync.build_parser().parse_args([
            "--full", "--start-date", "2026-01-01", "--end-date", "2026-01-14", *extra,
        ])
        result = sync.sync_sales(args)
        assert all(call[1]["json"]["PRODH_LIST"] == ["00100"] for call in http.calls)
        return result, client.db
    return run


def line(sales, quantity="3", **extra):
    return {"MANDT": "800", "AUFNR": "100", "VBELN": sales, "POSNR": "10",
            "PRODH": "00100", "GAMNG": quantity, **extra}


def response(*rows):
    return FakeResponse({"DATA": list(rows)})


def test_sales_aggregate_all_windows_before_writing(sales_run):
    result, db = sales_run([response(line("SO-A")), response(line("SO-B", "4"))])
    assert result["success"]
    doc = db["orders"].docs[0]
    assert {row["VBELN"] for row in doc["records"]} == {"SO-A", "SO-B"}
    assert doc["order_quantity"] == 7


def test_late_window_failure_leaves_source_target_untouched(sales_run):
    result, db = sales_run([response(line("SO-A")), FakeResponse({}, "timeout")])
    assert not result["success"]
    assert db["orders"].docs == []


def test_conflicting_key_across_windows_is_not_last_window_wins(sales_run):
    result, db = sales_run([response(line("SO-A")), response(line("SO-A", "9"))])
    assert not result["success"]
    assert db["orders"].docs == []


def test_filtered_response_without_product_field_and_other_product(sales_run):
    unknown = line("SO-U")
    unknown.pop("PRODH")
    result, db = sales_run([response(unknown), response(line("SO-X", PRODH="00200"))])
    assert result["sources"]["SG"]["scope_pending"] == 0
    assert result["sources"]["SG"]["out_of_scope"] == 1
    assert db["orders"].docs[0]["data"]["VBELN"] == "SO-U"
    assert not {"_prodh_scope", "_scope_rule_version", "_source_hash"} & db["orders"].docs[0].keys()


def test_full_never_deletes_absent_records(sales_run):
    first, db = sales_run([response(line("SO-A")), response()])
    db["orders"].docs.append({"_id": "SG:old", "source": "SG", "aufnr": "old"})
    second, db = sales_run([response(line("SO-A")), response()])
    assert first["success"] and second["success"]
    assert second["cleanup"] == "disabled_unverified_coverage"
    assert any(doc["_id"] == "SG:old" for doc in db["orders"].docs)


def test_old_unscoped_watermark_is_never_used():
    db = FakeDB()
    db["checkpoints"].docs.append({"_id": "sales_orders:SG", "last_success_date": "2026-10-26"})
    assert sync.read_watermark(db, "checkpoints", "SG") is None


def test_confirm_delete_cannot_override_missing_coverage(sales_run):
    with pytest.raises(ValueError, match="覆盖"):
        sales_run([response(), response()], "--replace-collection", "--confirm-delete")


def test_fresh_outside_scope_line_supersedes_stale_pending(sales_run):
    unknown = line("SO-U")
    unknown.pop("PRODH")
    _, db = sales_run([response(), response()])
    key = json.dumps(sync.status_line_key(unknown))
    sync.save_scope_pending(db, "sales:SG", key, unknown)
    second, db = sales_run([response(line("SO-U", PRODH="00200")), response()])
    assert second["sources"]["SG"]["out_of_scope"] == 1
    assert second["sources"]["SG"]["scope_pending"] == 0
    assert db["orders"].docs == []
    assert db["sync_scope_pending"].docs == []


def test_full_can_replace_legacy_order(sales_run):
    first, db = sales_run([response(line("SO-A")), response()])
    assert first["success"]
    for row in db["orders"].docs[0]["records"]:
        row.pop("PRODH")
        row["GAMNG"] = "1"
    db["orders"].docs[0]["data"] = dict(db["orders"].docs[0]["records"][0])
    fresh = line("SO-A", "4")
    fresh.pop("PRODH")
    result, db = sales_run([response(fresh), response()])

    assert result["success"]
    assert result["sources"]["SG"]["replaced_existing"] == 1
    assert db["orders"].docs[0]["order_quantity"] == 4
    assert "_prodh_scope" not in db["orders"].docs[0]


def test_full_replacement_discards_unmatched_old_lines(sales_run):
    first, db = sales_run([response(line("SO-A")), response()])
    assert first["success"]
    db["orders"].docs[0]["records"][0].pop("PRODH")
    db["orders"].docs[0]["data"].pop("PRODH")
    fresh = line("SO-B")
    fresh.pop("PRODH")
    result, db = sales_run([response(fresh), response()])

    assert result["success"]
    assert [row["VBELN"] for row in db["orders"].docs[0]["records"]] == ["SO-B"]


def test_filtered_response_needs_no_environment_override(sales_run, monkeypatch):
    monkeypatch.delenv("SAP_PRODH_FILTER_VERIFIED", raising=False)
    unknown = line("SO-U")
    unknown.pop("PRODH")
    result, db = sales_run([response(unknown), response()])
    assert result["success"]
    assert db["orders"].docs[0]["data"]["VBELN"] == "SO-U"


def test_source_replacement_excludes_unreturned_pending_and_preserves_other_orders(sales_run):
    stale = line("SO-STALE")
    stale.pop("PRODH")
    _, db = sales_run([response(), response()])
    key = json.dumps(sync.status_line_key(stale))
    sync.save_scope_pending(db, "sales:SG", key, stale)
    db["orders"].docs.append({"_id": "SG:old", "source": "SG", "aufnr": "old"})
    result, db = sales_run([response(line("SO-NEW")), response()])
    assert result["success"]
    order = next(doc for doc in db["orders"].docs if doc["_id"] == "SG:100")
    assert [row["VBELN"] for row in order["records"]] == ["SO-NEW"]
    assert any(doc["_id"] == "SG:old" for doc in db["orders"].docs)
    assert db["sync_scope_pending"].docs[0]["payload"]["VBELN"] == "SO-STALE"
