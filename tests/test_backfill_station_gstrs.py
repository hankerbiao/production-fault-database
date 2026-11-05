from datetime import date
from types import SimpleNamespace
from contextlib import contextmanager

from scripts.maintenance import backfill_station_gstrs as backfill
from scripts.sources.hana.hana_view_sync import VIEW_SPECS
from scripts.sync_runtime import Snapshot
from test_sync_sales_orders import FakeCollection


def test_backfill_preview_and_apply_only_insert_missing():
    target = FakeCollection([{"_source_key": "existing", "value": "keep"}])
    with Snapshot() as snapshot:
        snapshot.add("existing", {"value": "source"})
        snapshot.add("missing", {"value": "new"})
        preview = backfill.insert_missing(snapshot, target, "station", apply=False, batch_size=10, run_id="run")
        assert (preview["already_present"], preview["missing"], preview["inserted"]) == (1, 1, 0)
        assert len(target.docs) == 1
        applied = backfill.insert_missing(snapshot, target, "station", apply=True, batch_size=10, run_id="run")
        assert applied["inserted"] == 1
        assert target.find_one({"_source_key": "existing"})["value"] == "keep"
        inserted = target.find_one({"_source_key": "missing"})
        assert inserted["value"] == "new" and "_prodh_scope" not in inserted
        again = backfill.insert_missing(snapshot, target, "station", apply=True, batch_size=10, run_id="run")
        assert again["missing"] == 0 and again["inserted"] == 0


def test_stage_uses_inclusive_planned_date_range(monkeypatch):
    spec = VIEW_SPECS["Z_V_ZMES_T_001"]
    calls = []

    class Cursor:
        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchmany(self, _size):
            return []

        def close(self):
            pass

    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=Cursor)

    monkeypatch.setattr(backfill, "hana_connection", connection)
    with Snapshot() as snapshot:
        assert backfill.stage_source(snapshot, spec, "6210 C21", "TXM-OP210",
                                     date(2026, 10, 26), date(2026, 11, 1), 100) == 0
    assert '"GSTRS" >= TO_DATE' in calls[0][0] and '"GSTRS" < TO_DATE' in calls[0][0]
    assert calls[0][1] == ("6210 C21", "TXM-OP210", "2026-10-26", "2026-11-02")
