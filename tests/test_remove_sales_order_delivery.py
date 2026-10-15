from contextlib import contextmanager

import pytest

from scripts.maintenance import remove_sales_order_delivery as cleanup


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def estimated_document_count(self):
        return len(self.documents)

    def count_documents(self, query):
        return sum(all(doc.get(key) == value for key, value in query.items()) for doc in self.documents)

    def delete_many(self, query):
        self.documents[:] = [
            doc for doc in self.documents
            if not all(doc.get(key) == value for key, value in query.items())
        ]


class Database:
    name = "test"

    def __init__(self):
        self.collections = {
            cleanup.SOURCE_COLLECTION: Collection([{"_id": "delivery"}]),
            cleanup.SOURCE_COLLECTION + "_archive": Collection([{"_id": "archived"}]),
            cleanup.SUMMARY_COLLECTION: Collection([{"_id": "legacy-summary"}]),
            cleanup.SUMMARY_COLLECTION + "_build_123": Collection([{"_id": "summary"}]),
            cleanup.META_COLLECTION: Collection([{"_id": "active"}]),
            cleanup.SUMMARY_COLLECTION + "_build_unrelated": Collection([{"_id": "keep"}]),
            "sales_orders_sap": Collection([{"_id": "order"}]),
            "sync_checkpoints": Collection([
                {"_id": "hana_views:" + cleanup.VIEW_ID},
                {"_id": "hana_views:ZSGV_ZSD124"},
            ]),
            "sync_runs": Collection([
                {"view_id": cleanup.VIEW_ID}, {"view_id": "ZSGV_ZSD124"},
            ]),
            "sync_dataset_versions": Collection([
                {"_id": cleanup.VIEW_ID}, {"_id": "other"},
            ]),
            "sync_orchestrator_runs": Collection([{"_id": "keep-history"}]),
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
    for variable in (
        "SALES_ORDER_DELIVERY_COLLECTION", "SYNC_CHECKPOINT_COLLECTION", "SYNC_RUN_COLLECTION",
    ):
        monkeypatch.delenv(variable, raising=False)
    events = []

    @contextmanager
    def lock(*args):
        events.append(("enter", args[-1]))
        try:
            yield
        finally:
            events.append(("exit", args[-1]))

    monkeypatch.setattr(cleanup, "process_lock", lock)
    monkeypatch.setattr(cleanup, "resource_lock", lock)
    monkeypatch.setattr(cleanup, "check_leases", lambda **_kwargs: events.append(("verify", None)))
    return events


def test_preview_does_not_mutate_or_acquire_locks(locks):
    db = Database()
    names = db.list_collection_names()
    result = cleanup.remove_dataset(db)
    assert result["success"] and result["mode"] == "dry-run"
    assert result["collections"][cleanup.SOURCE_COLLECTION] == 1
    assert result["metadata"]["sync_runs"] == 1
    assert db.list_collection_names() == names
    assert db["sync_runs"].count_documents({}) == 2
    assert not db.dropped and not locks


def test_apply_only_removes_delivery_data_and_is_repeatable(locks):
    db = Database()
    result = cleanup.remove_dataset(db, apply=True)
    assert result["success"]
    assert len(db.dropped) == 5
    assert locks[0][0] == "enter"
    assert locks[1] == ("enter", cleanup.SOURCE_COLLECTION)
    assert locks[-2][0] == locks[-1][0] == "exit"
    assert db["sales_orders_sap"].count_documents({}) == 1
    assert db["sync_checkpoints"].documents == [{"_id": "hana_views:ZSGV_ZSD124"}]
    assert db["sync_runs"].documents == [{"view_id": "ZSGV_ZSD124"}]
    assert db["sync_dataset_versions"].documents == [{"_id": "other"}]
    assert db["sync_orchestrator_runs"].count_documents({}) == 1
    assert db[cleanup.SUMMARY_COLLECTION + "_build_unrelated"].count_documents({}) == 1
    repeated = cleanup.remove_dataset(db, apply=True)
    assert repeated["success"] and repeated["collections"] == {}
    assert not any(repeated["metadata"].values())


def test_active_writer_prevents_any_deletion(monkeypatch, locks):
    db = Database()

    @contextmanager
    def busy(*_args):
        raise RuntimeError("active writer")
        yield

    monkeypatch.setattr(cleanup, "resource_lock", busy)
    with pytest.raises(RuntimeError, match="active writer"):
        cleanup.remove_dataset(db, apply=True)
    assert not db.dropped
    assert db["sync_runs"].count_documents({}) == 2


def test_lost_lease_prevents_deletion(monkeypatch, locks):
    db = Database()

    def lost(**_kwargs):
        raise RuntimeError("lease lost")

    monkeypatch.setattr(cleanup, "check_leases", lost)
    with pytest.raises(RuntimeError, match="lease lost"):
        cleanup.remove_dataset(db, apply=True)
    assert not db.dropped


def test_custom_collection_requires_ownership_verification(monkeypatch, locks):
    monkeypatch.setenv("SALES_ORDER_DELIVERY_COLLECTION", "sales_orders_sap")
    db = Database()
    with pytest.raises(RuntimeError, match="ownership verification"):
        cleanup.remove_dataset(db, apply=True)
    assert not db.dropped


def test_empty_database_is_a_successful_noop(locks):
    db = Database()
    db.collections.clear()
    assert cleanup.remove_dataset(db, apply=True) == {
        "success": True, "mode": "apply", "database": "test",
        "collections": {}, "metadata": {},
    }
