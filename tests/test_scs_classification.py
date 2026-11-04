import pytest

from scripts import scs_classification as classification
from test_sync_sales_orders import FakeDB


def test_enqueues_old_and_new_service_before_source_mutation():
    db = FakeDB()
    classification.enqueue_change(db, {"service_uid": "OLD", "is_5000_company": True},
                                  {"service_uid": "NEW", "is_5000_company": False})
    assert {doc["_id"] for doc in db["sync_classification_pending"].docs} == {"OLD", "NEW"}


def test_recomputes_all_current_relations_and_revokes_last_association():
    db = FakeDB()
    db["doa"].docs = [
        {"_id": "a", "service_uid": "S", "service_order_keys": ["S"], "is_5000_company": False, "_prodh_scope": "00100"},
        {"_id": "b", "service_uid": "S", "service_order_keys": ["S"], "is_5000_company": True, "_prodh_scope": "00100"},
    ]
    db["changes"].docs = [{"_id": "c", "so_code": "S", "is_5000_company": False, "_prodh_scope": "00100"}]
    classification.enqueue(db, {"S"})
    classification.consume(db, "doa", "changes")
    assert db["changes"].docs[0]["is_5000_company"] is True
    db["doa"].docs.pop()
    classification.enqueue(db, {"S"})
    classification.consume(db, "doa", "changes")
    assert db["changes"].docs[0]["is_5000_company"] is False
    assert db["sync_classification_pending"].docs == []


def test_failed_propagation_preserves_work_and_generation_cas(monkeypatch):
    db = FakeDB()
    classification.enqueue(db, {"S"})
    old = db["sync_classification_pending"].docs[0]["generation"]
    db["changes"].docs = [{"_id": "c", "so_code": "S", "_prodh_scope": "00100"}]
    monkeypatch.setattr(db["changes"], "bulk_write", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="failed"):
        classification.consume(db, "doa", "changes")
    assert db["sync_classification_pending"].docs[0]["generation"] == old
    classification.enqueue(db, {"S"})
    db["sync_classification_pending"].delete_many({"_id": "S", "generation": old})
    assert len(db["sync_classification_pending"].docs) == 1
