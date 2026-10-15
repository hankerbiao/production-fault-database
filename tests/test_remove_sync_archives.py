from contextlib import contextmanager

import pytest

from scripts.maintenance import remove_sync_archives as cleanup


class Collection:
    def __init__(self, count):
        self.count = count

    def estimated_document_count(self):
        return self.count


class Database:
    name = "test"

    def __init__(self):
        self.collections = {
            "station_records_sap": Collection(10),
            "station_records_sap_archive": Collection(3),
            "repair_records_sap": Collection(20),
            "repair_records_sap_archive": Collection(2),
            "order_bom_postings_sap_archive": Collection(4),
            "sync_runs": Collection(8),
            "repair_order_backfill_audit": Collection(6),
            "unrelated_archive": Collection(5),
        }
        self.dropped = []

    def __getitem__(self, name):
        return self.collections[name]

    def list_collection_names(self):
        return list(self.collections)

    def drop_collection(self, name):
        self.dropped.append(name)
        del self.collections[name]


@pytest.fixture
def locks(monkeypatch):
    for variable in cleanup.DATASETS:
        monkeypatch.delenv(variable, raising=False)
    events = []

    @contextmanager
    def lock(_db, source):
        events.append(("enter", source))
        try:
            yield
        finally:
            events.append(("exit", source))

    monkeypatch.setattr(cleanup, "resource_lock", lock)
    monkeypatch.setattr(cleanup, "check_leases", lambda **_kwargs: events.append(("verify", None)))
    return events


def test_preview_leaves_data_and_locks_untouched(locks):
    db = Database()
    before = db.list_collection_names()
    result = cleanup.remove_archives(db)
    assert result["success"] and result["mode"] == "dry-run"
    assert sum(result["collections"].values()) == 9
    assert result["deleted_documents"] == 0
    assert db.list_collection_names() == before
    assert not locks and not db.dropped


def test_apply_removes_only_known_archives_and_is_repeatable(locks):
    db = Database()
    result = cleanup.remove_archives(db, apply=True)
    assert result["success"] and result["deleted_documents"] == 9
    assert set(db.dropped) == {
        "station_records_sap_archive", "repair_records_sap_archive",
        "order_bom_postings_sap_archive",
    }
    assert db["station_records_sap"].count == 10
    assert db["repair_records_sap"].count == 20
    assert db["sync_runs"].count == 8
    assert db["repair_order_backfill_audit"].count == 6
    assert db["unrelated_archive"].count == 5
    first_verify = next(i for i, event in enumerate(locks) if event[0] == "verify")
    assert len([event for event in locks[:first_verify] if event[0] == "enter"]) == len(cleanup.DATASETS)
    repeated = cleanup.remove_archives(db, apply=True)
    assert repeated["collections"] == {} and repeated["deleted_documents"] == 0


@pytest.mark.parametrize("failure", ["lock", "lease"])
def test_busy_or_lost_lock_prevents_all_deletions(monkeypatch, locks, failure):
    db = Database()
    if failure == "lock":
        @contextmanager
        def busy(*_args):
            raise RuntimeError("active writer")
            yield
        monkeypatch.setattr(cleanup, "resource_lock", busy)
    else:
        def lost(**_kwargs):
            raise RuntimeError("lease lost")
        monkeypatch.setattr(cleanup, "check_leases", lost)
    with pytest.raises(RuntimeError):
        cleanup.remove_archives(db, apply=True)
    assert not db.dropped


def test_configured_collection_archives_are_included(monkeypatch, locks):
    monkeypatch.setenv("STATION_COLLECTION", "custom_stations")
    db = Database()
    db.collections["custom_stations"] = Collection(30)
    db.collections["custom_stations_archive"] = Collection(7)
    result = cleanup.remove_archives(db, apply=True)
    assert result["deleted_documents"] == 16
    assert db["custom_stations"].count == 30
    assert ("enter", "custom_stations") in locks


def test_active_dataset_collision_is_rejected(monkeypatch, locks):
    monkeypatch.setenv("REPAIR_COLLECTION", "station_records_sap_archive")
    db = Database()
    with pytest.raises(RuntimeError, match="overlaps"):
        cleanup.remove_archives(db, apply=True)
    assert not db.dropped and not locks
