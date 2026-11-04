from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

from scripts.sources import scs_doa_sync as doa, scs_change_sync as change
from scripts.sources.hana import hana_view_sync as hana
from test_sync_sales_orders import FakeDB, FakeMongoClient


def setup_scs(monkeypatch, module, db):
    client = FakeMongoClient()
    client.db = db
    monkeypatch.setattr(module, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(module, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(module, "MongoClient", lambda *a, **k: client)
    monkeypatch.setattr(module, "resource_locks", lambda *a: nullcontext())
    monkeypatch.setattr(module, "login", lambda *a: None)
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    monkeypatch.setenv("SCS_DOA_COLLECTION", "doa")
    monkeypatch.setenv("SCS_CHANGE_COLLECTION", "changes")
    monkeypatch.setenv("SYNC_CHECKPOINT_COLLECTION", "checkpoints")
    return SimpleNamespace(full=True, dry_run=False, page_size=500, lookback_days=2,
                           start_date="2026-01-01", end_date="2026-10-27")


def test_doa_requires_verified_order_and_retries_unknown_scope(monkeypatch):
    db = FakeDB()
    args = setup_scs(monkeypatch, doa, db)
    row = {"doa_code": "D1", "service_uid": "S1", "declare_time": "2026-10-26"}
    monkeypatch.setattr(doa, "iter_list", lambda *a: [[row]])
    monkeypatch.setattr(doa, "fetch_detail", lambda *a: {"fields": {"订单号": "XNC1"}})
    first = doa.sync(args)
    assert first["scope_pending"] == 1
    assert db["doa"].docs == []
    assert len(db["sync_scope_pending"].docs) == 1
    db["orders"].docs = [{"source": "SG", "data": {"AUFNR": "1", "VBELN": "XNC1", "PRODH": "00100"}}]
    monkeypatch.setattr(doa, "iter_list", lambda *a: [])
    second = doa.sync(args)
    assert second["in_scope"] == 1
    assert db["doa"].docs[0]["_prodh_scope"] == "00100"
    assert db["sync_scope_pending"].docs == []


def test_change_full_keeps_missing_records_and_rejects_company_as_product_proof(monkeypatch):
    db = FakeDB()
    args = setup_scs(monkeypatch, change, db)
    db["doa"].docs = [{"service_uid": "S1", "is_5000_company": True}]
    db["changes"].docs = [{"_id": "old", "_source": "scs", "create_time": "2026-01-01"}]
    monkeypatch.setattr(change, "iter_changes", lambda *a: [[{"so_code": "S1", "create_time": "2026-10-26"}]])
    result = change.sync(args)
    assert result["scope_pending"] == 1
    assert [doc["_id"] for doc in db["changes"].docs] == ["old"]
    assert result["cleanup"] == "disabled_unverified_coverage"


def test_change_admits_verified_service_and_batches_existing_lookup(monkeypatch):
    db = FakeDB()
    args = setup_scs(monkeypatch, change, db)
    db["doa"].docs = [{"service_uid": "S1", "is_5000_company": True, "_prodh_scope": "00100"}]
    rows = [{"so_code": "S1", "part_sn": str(i), "create_time": "2026-10-26"} for i in range(1000)]
    monkeypatch.setattr(change, "iter_changes", lambda *a: [rows])
    monkeypatch.setenv("SCS_WRITE_BATCH_SIZE", "100")
    first = change.sync(args)
    assert first["in_scope"] == 1000 and first["lookup_batches"] == 10
    args.full = False
    second = change.sync(args)
    assert second["upserted"] == 0 and second["unchanged"] == 1000


def test_failed_doa_refresh_preserves_old_business_fields_and_queues_new_row(monkeypatch):
    db = FakeDB()
    args = setup_scs(monkeypatch, doa, db)
    db["orders"].docs = [{"source": "SG", "data": {"AUFNR": "1", "VBELN": "XNC1", "PRODH": "00100"}}]
    row = {"doa_code": "D1", "service_uid": "S1", "declare_time": "2026-10-26"}
    monkeypatch.setattr(doa, "iter_list", lambda *a: [[row]])
    monkeypatch.setattr(doa, "fetch_detail", lambda *a: {"fields": {"订单号": "XNC1"}})
    assert doa.sync(args)["success"]
    old = dict(db["doa"].docs[0])
    monkeypatch.setattr(doa, "iter_list", lambda *a: [[{**row, "service_uid": "S2"}]])

    def fail(*args):
        raise RuntimeError("detail unavailable")

    monkeypatch.setattr(doa, "fetch_detail", fail)
    result = doa.sync(args)
    stored = db["doa"].docs[0]
    assert not result["success"]
    assert stored["service_uid"] == "S1"
    assert stored["details"] == old["details"]
    assert stored["_detail_fetched_at"] == old["_detail_fetched_at"]
    assert stored["_detail_pending"]
    assert db["sync_scope_pending"].docs[0]["payload"]["service_uid"] == "S2"


def test_unchanged_doa_clears_previously_persisted_pending(monkeypatch):
    from scripts.sync_runtime import save_scope_pending

    db = FakeDB()
    args = setup_scs(monkeypatch, doa, db)
    db["orders"].docs = [{"source": "SG", "data": {"AUFNR": "1", "VBELN": "XNC1", "PRODH": "00100"}}]
    row = {"doa_code": "D1", "service_uid": "S1", "declare_time": "2026-10-26"}
    monkeypatch.setattr(doa, "iter_list", lambda *a: [[row]])
    monkeypatch.setattr(doa, "fetch_detail", lambda *a: {"fields": {"订单号": "XNC1"}})
    doa.sync(args)
    save_scope_pending(db, doa.CHECKPOINT_ID, doa.source_key(row), row)
    args.full = False
    result = doa.sync(args)
    assert result["unchanged"] == 1
    assert db["sync_scope_pending"].docs == []


def test_hana_component_prodh_does_not_override_main_order_scope(monkeypatch):
    db = FakeDB()
    db["orders"].docs = [
        {"source": "SG", "data": {"AUFNR": "A", "VBELN": "S1", "PRODH": "00100"}},
        {"source": "SG", "data": {"AUFNR": "B", "VBELN": "S2", "PRODH": "00200"}},
    ]
    client = FakeMongoClient()
    client.db = db
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    monkeypatch.setattr(hana, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(hana, "MongoClient", lambda *a, **k: client)
    values = [("HOST-A", "PART-A", "A", "x", "00200"),
              ("HOST-B", "PART-B", "B", "y", "00100"),
              ("HOST-U", "PART-U", "U", "z", "00100")]
    cursor = SimpleNamespace(execute=lambda *a: None, close=lambda: None)
    cursor.fetchmany = lambda size: values if values else []
    calls = 0

    def fetch(size):
        nonlocal calls
        calls += 1
        return values if calls == 1 else []
    cursor.fetchmany = fetch

    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(hana, "hana_connection", connection)
    spec = hana.VIEW_SPECS["ZSGV_ZPP_SERNOLIST"]
    result = hana.sync_view(spec, mode="full", start_date=None, batch_size=100,
                            lookback_days=7, dry_run=False)
    assert (result["in_scope"], result["out_of_scope"], result["scope_pending"]) == (2, 0, 1)
    assert {row["ZCODE_HEAD"] for row in db[spec.collection].docs} == {"HOST-A", "HOST-B"}
    assert db["sync_scope_pending"].docs[0]["payload"]["ZCODE_HEAD"] == "HOST-U"


def test_bom_sync_matches_sales_and_queues_unknown_orders(monkeypatch):
    db = FakeDB()
    client = FakeMongoClient()
    client.db = db
    monkeypatch.setenv('MONGODB_DATABASE', 'test')
    monkeypatch.setenv('TARGET_COLLECTION', 'orders')
    monkeypatch.setattr(hana, 'mongo_uri', lambda: 'mongodb://test')
    monkeypatch.setattr(hana, 'MongoClient', lambda *a, **k: client)
    spec = hana.VIEW_SPECS['ZSGV_ZSD124']
    rows = [{field: '' for field in spec.columns} for _ in range(2)]
    for row, ident, order in zip(rows, ('1', '2'), ('OTHER', 'UNKNOWN')):
        row.update(MANDT='800', MBLNR=ident, MJAHR='2026', ZEILE='1', AUFNR_1=order, CPX='5000公司')
    db['orders'].docs = [{'source': 'SG', 'data': {'AUFNR': 'OTHER', 'PRODH': '00200'}}]
    pending = dict(rows[1], MBLNR='3', AUFNR_1='PENDING')
    dataset = 'hana:ZSGV_ZSD124'
    pending_key = hana.source_key(pending, spec)
    db['sync_scope_pending'].docs = [{'_id': f'{dataset}:{pending_key}', 'dataset': dataset, 'key': pending_key, 'payload': pending}]
    cursor = SimpleNamespace(execute=lambda *a: None, close=lambda: None)
    remaining = list(rows)
    def fetch(size):
        result = remaining[:size]
        del remaining[:size]
        return [tuple(row.get(field) for field in spec.columns) for row in result]
    cursor.fetchmany = fetch
    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(hana, 'hana_connection', connection)
    result = hana.sync_view(spec, mode='full', start_date=None, batch_size=10, lookback_days=7, dry_run=False)
    assert result['upserted'] == 1
    assert {row['AUFNR_1'] for row in db[spec.collection].docs} == {'OTHER'}
    assert {row['payload']['AUFNR_1'] for row in db['sync_scope_pending'].docs} == {'UNKNOWN', 'PENDING'}
    assert db[spec.collection].docs[0]['_scope_rule_version'] == 2
