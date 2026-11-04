from datetime import date

import pytest

from scripts.sync.sync_sales_orders import status_partition
from test_sync_sales_orders import FakeDB


def test_status_partition_recent_coverage_and_independent_history_cursor(monkeypatch):
    monkeypatch.setenv("STATUS_RECENT_DAYS", "30")
    monkeypatch.setenv("STATUS_HISTORY_WINDOWS", "3")
    monkeypatch.setenv("STATUS_HISTORY_MAX_INTERVAL_DAYS", "14")
    monkeypatch.setenv("SYNC_RUNS_PER_DAY", "1")
    db = FakeDB()
    windows, checkpoint, next_index = status_partition(db, "SG", date(2026, 1, 1), date(2026, 10, 27), 7)
    assert len(windows) == 8
    assert windows[-1] == (date(2026, 10, 26), date(2026, 10, 27))
    assert next_index == 3
    db["sync_checkpoints"].update_one({"_id": checkpoint}, {"$set": {"next_index": next_index}}, upsert=True)
    resumed, _, _ = status_partition(db, "SG", date(2026, 1, 1), date(2026, 10, 27), 7)
    kk, _, _ = status_partition(db, "KK", date(2026, 1, 1), date(2026, 10, 27), 7)
    assert resumed[0][0] == date(2026, 1, 22)
    assert kk[0][0] == date(2026, 1, 1)


def test_history_budget_must_meet_declared_maximum_interval(monkeypatch):
    monkeypatch.setenv("STATUS_RECENT_DAYS", "30")
    monkeypatch.setenv("STATUS_HISTORY_WINDOWS", "1")
    monkeypatch.setenv("STATUS_HISTORY_MAX_INTERVAL_DAYS", "7")
    monkeypatch.setenv("SYNC_RUNS_PER_DAY", "1")
    with pytest.raises(ValueError, match="预算"):
        status_partition(FakeDB(), "SG", date(2026, 1, 1), date(2026, 10, 27), 7)


def test_default_keeps_full_history_without_mutating_progress(monkeypatch):
    monkeypatch.delenv("STATUS_RECENT_DAYS", raising=False)
    db = FakeDB()
    windows, checkpoint, _ = status_partition(db, "SG", date(2026, 1, 1), date(2026, 1, 31), 7)
    assert len(windows) == 5 and checkpoint is None
    assert db.collections == {}


def test_failed_historical_read_does_not_advance_cursor(monkeypatch):
    from scripts.sync import sync_sales_orders as sync
    from test_sync_sales_orders import FakeHTTPClient, FakeResponse

    monkeypatch.setenv("STATUS_RECENT_DAYS", "30")
    monkeypatch.setenv("STATUS_HISTORY_WINDOWS", "3")
    monkeypatch.setenv("STATUS_HISTORY_MAX_INTERVAL_DAYS", "14")
    monkeypatch.setenv("SYNC_RUNS_PER_DAY", "1")
    monkeypatch.setattr(sync, "SOURCES", {"SG": "http://sg"})
    db = FakeDB()
    _, identity, _ = status_partition(db, "SG", date(2026, 1, 1), date(2026, 10, 27), 7)
    db["sync_checkpoints"].update_one({"_id": identity}, {"$set": {"next_index": 3}}, upsert=True)
    http = FakeHTTPClient({"http://sg": [FakeResponse({"DATA": []}), FakeResponse({}, "timeout")]})
    with pytest.raises(RuntimeError, match="timeout"):
        sync.refresh_order_statuses(db, http, start=date(2026, 1, 1), end=date(2026, 10, 27),
                                   prodh=["00100"], apply=True, run_id="failed")
    assert db["sync_checkpoints"].docs == [{"_id": identity, "next_index": 3}]
