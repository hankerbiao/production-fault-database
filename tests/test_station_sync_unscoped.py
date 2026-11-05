from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts.maintenance import clean_order_boards as boards
from scripts.sources.hana import hana_view_sync as hana
from scripts.sync import station_records
from test_sync_sales_orders import FakeDB, FakeMongoClient


@pytest.fixture
def station_source(monkeypatch):
    db = FakeDB()
    db["orders"].docs = [{"source": "SG", "data": {"AUFNR": "KNOWN"}}]
    client = FakeMongoClient()
    client.db = db
    spec = hana.VIEW_SPECS["Z_V_ZMES_T_001"]
    rows = []
    queries = []

    class Cursor:
        def execute(self, sql, params):
            queries.append((sql, params))
            self.remaining = list(rows)

        def fetchmany(self, size):
            batch, self.remaining = self.remaining[:size], self.remaining[size:]
            return [tuple(row.get(field) for field in spec.columns) for row in batch]

        def close(self):
            pass

    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    monkeypatch.setenv("SYNC_CHECKPOINT_COLLECTION", "checkpoints")
    monkeypatch.setattr(hana, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(hana, "MongoClient", lambda *a, **k: client)
    monkeypatch.setattr(hana, "hana_connection", connection)
    return db, spec, rows, queries


def station_row(identity, order="UNKNOWN", started="2026-10-27 10:00:00"):
    return {
        "HISTROYID": identity, "SPEC": "OP10", "SPEC_TIME": started,
        "PCODE": f"SN-{identity}", "OCODE": f"OC-{identity}",
        "MANDT": "800", "AUFNR": order, "ACTUAL_START_TIME": started,
    }


@pytest.mark.parametrize(("arguments", "expected"), [([], 5), (["--lookback-days", "8"], 8)])
def test_station_default_lookback_is_five_days(monkeypatch, arguments, expected):
    seen = []
    monkeypatch.setenv("SYNC_LOOKBACK_DAYS", "2")
    monkeypatch.setattr("sys.argv", ["station_records.py", *arguments])
    monkeypatch.setattr(station_records, "run_hana_table", lambda _id, _kind, args: seen.append(args.lookback_days) or {"success": True})
    monkeypatch.setattr(station_records, "print_result", lambda _result: 0)
    assert station_records.main() == 0
    assert seen == [expected]


def test_station_admits_all_products_without_sales_reference_idempotently(station_source):
    db, spec, rows, _ = station_source
    db["orders"].docs = []
    rows.extend([station_row("unknown"), station_row("other", "OTHER")])
    first = hana.sync_view(
        spec, mode="incremental", start_date="20261020", end_date="20261028",
        batch_size=100, lookback_days=7, dry_run=False,
    )
    assert first["upserted"] == 2
    assert len(db[spec.collection].docs) == 2
    assert db["sync_scope_pending"].docs == []
    assert all("_prodh_scope" not in row and "_scope_rule_version" not in row
               for row in db[spec.collection].docs)
    second = hana.sync_view(
        spec, mode="incremental", start_date="20261020", end_date="20261028",
        batch_size=100, lookback_days=7, dry_run=False,
    )
    assert second["upserted"] == 0
    assert len(db[spec.collection].docs) == 2


def test_station_migrates_legacy_watermark_to_operation_time(station_source):
    db, spec, rows, queries = station_source
    db["checkpoints"].docs = [{
        "_id": "hana_views:Z_V_ZMES_T_001", "watermark": "2026-10-27 09:53:51",
    }]
    row = station_row("new")
    row["SPEC_TIME"] = "2026-10-28 08:00:00"
    rows.append(row)
    hana.sync_view(
        spec, mode="incremental", start_date=None, end_date="20261028",
        batch_size=100, lookback_days=7, dry_run=False,
    )
    assert '"SPEC_TIME" >= ?' in queries[0][0]
    assert '"SPEC_TIME" IS NULL' in queries[0][0]
    assert queries[0][1] == ("2026-10-20 09:53:51", "2026-10-20 09:53:51", "2026-10-29 00:00:00", "2026-10-29 00:00:00")
    checkpoints = {item["_id"]: item for item in db["checkpoints"].docs}
    assert checkpoints["hana_views:Z_V_ZMES_T_001"]["watermark"] == "2026-10-27 09:53:51"
    assert checkpoints["hana_views:Z_V_ZMES_T_001:spec_time:v1"]["watermark"] == "2026-10-28 08:00:00"

    hana.sync_view(spec, mode="incremental", start_date=None, end_date="20261029",
                   batch_size=100, lookback_days=2, dry_run=False)
    assert queries[1][1] == ("2026-10-26 08:00:00", "2026-10-26 08:00:00", "2026-10-30 00:00:00", "2026-10-30 00:00:00")


def test_station_missing_watermark_requires_explicit_start(station_source):
    _, spec, _, queries = station_source
    with pytest.raises(RuntimeError, match="检查点"):
        hana.sync_view(
            spec, mode="incremental", start_date=None,
            batch_size=100, lookback_days=7, dry_run=False,
        )
    assert queries == []


def test_station_historical_full_does_not_advance_checkpoint_past_end_date(station_source):
    db, spec, rows, _ = station_source
    db["checkpoints"].docs = [{"_id": "hana_views:Z_V_ZMES_T_001:spec_time:v1",
                               "watermark": "2026-10-26 08:00:00"}]
    row = station_row("later")
    row["SPEC_TIME"] = "2026-11-03 08:00:00"
    rows.append(row)
    result = hana.sync_view(spec, mode="full", start_date="20261020", end_date="20261028",
                            batch_size=100, lookback_days=7, dry_run=False)
    assert result["upserted"] == 1
    checkpoint = db["checkpoints"].find_one({"_id": "hana_views:Z_V_ZMES_T_001:spec_time:v1"})
    assert checkpoint["watermark"] == "2026-10-26 08:00:00"


def test_bom_missing_watermark_requires_explicit_start(station_source):
    _, _, _, queries = station_source
    spec = hana.VIEW_SPECS["ZSGV_ZSD124"]
    with pytest.raises(RuntimeError, match="检查点"):
        hana.sync_view(
            spec, mode="incremental", start_date=None,
            batch_size=100, lookback_days=7, dry_run=False,
        )
    assert queries == []


def test_station_recovers_pending_rows_before_clearing_them(station_source):
    db, spec, rows, _ = station_source
    db["orders"].docs = []
    row = station_row("pending", started="2026-10-19 10:00:00")
    dataset = "hana:Z_V_ZMES_T_001"
    key = hana.source_key(row, spec)
    db["sync_scope_pending"].docs = [{
        "_id": f"{dataset}:{key}", "dataset": dataset, "key": key, "payload": row,
    }, {"_id": "unrelated", "dataset": "hana:ZSGV_ZSD124"}]
    rows.append(station_row("fresh"))
    hana.sync_view(
        spec, mode="incremental", start_date="20261020",
        batch_size=100, lookback_days=7, dry_run=False,
    )
    assert {row["PCODE"] for row in db[spec.collection].docs} == {"SN-pending", "SN-fresh"}
    assert db["sync_scope_pending"].docs == [{"_id": "unrelated", "dataset": "hana:ZSGV_ZSD124"}]


@pytest.mark.parametrize("has_order", [False, True])
def test_station_cleanup_without_scope_keeps_unmatched_and_fills_known_orders(has_order):
    db = FakeDB()
    db["station"].docs = [{
        "_id": "row", "_source_view": "Z_V_ZMES_T_001", "AUFNR": "A", "KDAUF": "",
    }]
    if has_order:
        db["orders"].docs = [{
            "source": "SG", "data": {"AUFNR": "A", "VBELN": "S1", "PRODH": "00200"},
        }]
    result = boards.process_board(
        db, boards.SPECS["station"], "station", "orders", apply=True,
        batch_size=100, limit=None, from_date=None, to_date=None, progress=False,
    )
    assert result["success"]
    assert result["scanned"] == 1
    assert result["deleted"] == 0
    assert db["station"].docs[0]["KDAUF"] == ("S1" if has_order else "")
