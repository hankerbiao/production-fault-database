from scripts.maintenance.enforce_prodh_scope import migrate
from test_sync_sales_orders import FakeDB


def test_preview_is_read_only_and_mixed_lines_are_recomputed():
    db = FakeDB()
    db["orders"].docs = [{
        "_id": "SG:1", "source": "SG", "aufnr": "1",
        "records": [
            {"AUFNR": "1", "VBELN": "A", "PRODH": "00100", "GAMNG": "3"},
            {"AUFNR": "1", "VBELN": "B", "PRODH": "00200", "GAMNG": "7"},
        ],
    }]
    result = migrate(db, {"sales": "orders"}, apply=False)
    assert result["sales"]["would_trim"] == 1
    assert len(db["orders"].docs[0]["records"]) == 2
    assert "sync_scope_migrations" not in db.collections
    result = migrate(db, {"sales": "orders"}, apply=True)
    assert result["migration_complete"]
    assert db["orders"].docs[0]["order_quantity"] == 3
    assert db["orders"].docs[0]["data"]["VBELN"] == "A"
    assert len(db["sync_scope_migrations"].docs[0]["before"]["records"]) == 2


def test_unknown_is_retained_and_non_scope_deleted_only_with_evidence():
    db = FakeDB()
    db["orders"].docs = [
        {"_id": "SG:1", "source": "SG", "data": {"AUFNR": "1", "PRODH": "00200"}},
        {"_id": "SG:2", "source": "SG", "data": {"AUFNR": "2"}},
    ]
    result = migrate(db, {"sales": "orders"}, apply=True)
    assert not result["migration_complete"]
    assert [row["_id"] for row in db["orders"].docs] == ["SG:2"]
    assert result["sales"]["scope_pending"] == 1
    assert len(db["sync_scope_pending"].docs) == 1


def test_resolved_migration_pending_is_cleared_on_rerun():
    db = FakeDB()
    db["orders"].docs = [
        {"_id": "SG:1", "source": "SG", "data": {"AUFNR": "1", "VBELN": "S1"}},
    ]
    first = migrate(db, {"sales": "orders"}, apply=True)
    assert not first["migration_complete"]
    db["orders"].docs[0]["data"]["PRODH"] = "00100"
    second = migrate(db, {"sales": "orders"}, apply=True)
    assert second["migration_complete"]
    assert db["sync_scope_pending"].docs == []


def test_repair_migration_uses_original_orders_and_drops_invalid_backfill():
    db = FakeDB()
    db["orders"].docs = [
        {"_id": "KK:GOOD", "source": "KK", "data": {"AUFNR": "GOOD", "VBELN": "SGOOD", "PRODH": "00100"}},
        {"_id": "KK:BAD", "source": "KK", "data": {"AUFNR": "BAD", "VBELN": "SBAD", "PRODH": "00200"}},
    ]
    db["station"].docs = [{"_id": "station", "MANDT": "600", "PCODE": "PC1",
                          "AUFNR": "GOOD", "KDAUF": "SGOOD"}]
    db["repair"].docs = [{"_id": "repair", "MANDT": "600", "PCODE": "PC1",
                         "_source_orders": {"AUFNR": "", "VBELN": ""},
                         "AUFNR": "BAD", "VBELN": "SBAD", "GSTRS": "2026-01-01"}]
    result = migrate(db, {"sales": "orders", "station": "station", "repair": "repair"}, apply=True)
    assert result["repair"]["deleted"] == 0
    document = db["repair"].docs[0]
    assert document["_prodh_scope"] == "00100"
    assert (document["AUFNR"], document["VBELN"], document["GSTRS"]) == ("", "", "")
