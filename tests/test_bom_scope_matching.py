from scripts.maintenance.backfill_order_bom_planned_start import backfill
from test_sync_sales_orders import FakeDB


def test_bom_does_not_take_date_from_an_unrelated_order_pair():
    db = FakeDB()
    db["sales"].docs = [{
        "source": "SG", "data": {"AUFNR": "1", "VBELN": "S1", "GSTRS": "20261001", "PRODH": "00100"},
    }]
    db["bom"].docs = [{
        "_id": "bom-1", "AUFNR_1": "1", "VBELN_EX": "S2", "_prodh_scope": "00100",
    }]
    result = backfill(db, bom_collection_name="bom", sales_collection_name="sales",
                      batch_size=100, preview_limit=10, apply=True)
    assert result["write_attempted"] == 0
    assert "GSTRS" not in db["bom"].docs[0]


def test_bom_uses_client_and_both_orders_to_resolve_date():
    db = FakeDB()
    db["sales"].docs = [
        {"source": source, "records": [
            {"AUFNR": "1", "MANDT": client, "VBELN": "S1", "GSTRS": date, "PRODH": "00100"},
        ]}
        for source, client, date in [("SG", "800", "20261001"), ("KK", "600", "20261002")]
    ]
    db["bom"].docs = [{
        "_id": "bom-1", "AUFNR_1": "1", "VBELN_EX": "S1", "MANDT": "800", "_prodh_scope": "00100",
    }]
    backfill(db, bom_collection_name="bom", sales_collection_name="sales",
             batch_size=100, preview_limit=10, apply=True)
    assert db["bom"].docs[0]["GSTRS"] == "2026-10-01"


def test_bom_backfill_accepts_unscoped_other_product():
    db = FakeDB()
    db['sales'].docs = [{'source': 'SG', 'data': {'MANDT': '800', 'AUFNR': '1', 'VBELN': 'S1', 'GSTRS': '20261001', 'PRODH': '00200'}}]
    db['bom'].docs = [{'_id': 'bom-1', 'MANDT': '800', 'AUFNR_1': '1', 'VBELN_EX': 'S1'}]
    backfill(db, bom_collection_name='bom', sales_collection_name='sales', batch_size=100, preview_limit=10, apply=True)
    assert db['bom'].docs[0]['GSTRS'] == '2026-10-01'
