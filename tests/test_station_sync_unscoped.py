from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts.maintenance import clean_order_boards as boards
from scripts.sources.hana import hana_view_sync as hana
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


def test_station_admits_sales_order_and_queues_unknown_idempotently(station_source):
    db, spec, rows, _ = station_source
    db["orders"].docs = [
        {"source": "SG", "data": {"AUFNR": "OTHER", "PRODH": "00200"}},
    ]
    rows.extend([station_row("unknown"), station_row("other", "OTHER")])
    first = hana.sync_view(
        spec, mode="incremental", start_date="20261020", end_date="20261028",
        batch_size=100, lookback_days=7, dry_run=False,
    )
    assert first["upserted"] == 1
    assert len(db[spec.collection].docs) == 1
    assert len(db["sync_scope_pending"].docs) == 1
    assert db[spec.collection].docs[0]["_scope_rule_version"] == 2
    second = hana.sync_view(
        spec, mode="incremental", start_date="20261020", end_date="20261028",
        batch_size=100, lookback_days=7, dry_run=False,
    )
    assert second["upserted"] == 0
    assert len(db[spec.collection].docs) == 1


def test_station_resumes_and_commits_legacy_watermark(station_source):
    db, spec, rows, queries = station_source
    db["checkpoints"].docs = [{
        "_id": "hana_views:Z_V_ZMES_T_001", "watermark": "2026-10-27 09:53:51",
    }]
    rows.append(station_row("new"))
    hana.sync_view(
        spec, mode="incremental", start_date=None, end_date="20261028",
        batch_size=100, lookback_days=7, dry_run=False,
    )
    assert '"ACTUAL_START_TIME" >= ?' in queries[0][0]
    assert queries[0][1] == ("2026-10-20 09:53:51", "2026-10-28 23:59:59")
    assert len(db["checkpoints"].docs) == 1
    assert db["checkpoints"].docs[0]["_id"] == "hana_views:Z_V_ZMES_T_001"
    assert db["checkpoints"].docs[0]["watermark"] == "2026-10-27 10:00:00"


def test_station_missing_watermark_requires_explicit_start(station_source):
    _, spec, _, queries = station_source
    with pytest.raises(RuntimeError, match="检查点"):
        hana.sync_view(
            spec, mode="incremental", start_date=None,
            batch_size=100, lookback_days=7, dry_run=False,
        )
    assert queries == []


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
    db["orders"].docs.append({"source": "SG", "data": {"AUFNR": "UNKNOWN"}})
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
