from datetime import date
from types import SimpleNamespace

import pytest

import clean_orphan_repair_records as cleaner


class Cursor:
    def __init__(self, docs):
        self.docs = docs
        self.requested_batch_size = None

    def batch_size(self, size):
        self.requested_batch_size = size
        return self

    def sort(self, _field, _direction):
        self.docs = sorted(self.docs, key=lambda document: str(document.get("_id", "")))
        return self

    def __iter__(self):
        return iter(self.docs)

    def close(self):
        pass


class Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.find_filter = None
        self.deleted_filters = []

    def find(self, query, projection):
        self.find_filter = (query, projection)
        return Cursor(self.docs)

    def delete_many(self, query):
        self.deleted_filters.append(query)
        if "$or" in query:
            candidates = {
                (condition.get("_id"), condition.get("VBELN"))
                for condition in query["$or"]
            }
            keep = lambda doc: (doc.get("_id"), doc.get("VBELN")) not in candidates
        else:
            ids = set(query["_id"]["$in"])
            keep = lambda doc: doc.get("_id") not in ids
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if keep(doc)]
        return SimpleNamespace(deleted_count=before - len(self.docs))


class DB:
    def __init__(self, orders, repairs):
        self.collections = {"orders": orders, "repairs": repairs}

    def __getitem__(self, name):
        return self.collections[name]


def test_normalize_and_sales_order_values():
    assert cleaner.normalize_order(None) == ""
    assert cleaner.normalize_order("  SO-1 ") == "SO-1"
    assert cleaner.sales_order_vbelns(Collection(docs=[
        {"data": {"VBELN": None}}, {"data": {"VBELN": " SO-1 "}},
        {"data": {"VBELN": 12}}, {"data": {"VBELN": ""}},
    ])) == {"SO-1", "12"}


def test_dry_run_counts_empty_and_unknown_without_deleting():
    orders = Collection(docs=[{"data": {"VBELN": " SO-1 "}}])
    repairs = Collection([
        {"_id": "r1", "VBELN": "SO-1"},
        {"_id": "r2", "VBELN": " SO-2 "},
        {"_id": "r3", "VBELN": None},
    ])
    result = cleaner.clean_records(
        DB(orders, repairs), "repairs", "orders", "runs", apply=False,
        batch_size=2, limit=None, source_view="ZSGV_ZZT_WLJL", all_source_views=False,
        from_date=date(2026, 1, 1), to_date=date(2026, 1, 31), run_id="run-1",
    )
    assert result["scanned"] == 3
    assert result["empty_sales_order"] == 1
    assert result["unmatched_sales_order"] == 1
    assert result["orphan_records"] == 2
    assert result["deleted"] == 0
    assert repairs.docs and not repairs.deleted_filters
    assert repairs.find_filter[0] == {"_source_view": "ZSGV_ZZT_WLJL", "ZDATE_WX": {"$gte": "20260101", "$lte": "20260131"}}


def test_apply_deletes_in_batches_and_honors_limit():
    orders = Collection(docs=[{"data": {"VBELN": "SO-1"}}])
    repairs = Collection([
        {"_id": "r1", "VBELN": "SO-X"},
        {"_id": "r2", "VBELN": "SO-Y"},
        {"_id": "r3", "VBELN": "SO-1"},
    ])
    result = cleaner.clean_records(
        DB(orders, repairs), "repairs", "orders", "runs", apply=True,
        batch_size=1, limit=2, source_view="ZSGV_ZZT_WLJL", all_source_views=False,
        from_date=None, to_date=None, run_id="run-2",
    )
    assert result["unmatched_sales_order"] == 2
    assert result["orphan_records"] == 2
    assert result["deleted"] == 2
    assert {doc["_id"] for doc in repairs.docs} == {"r3"}
    assert len(repairs.deleted_filters) == 2


def test_apply_rechecks_orders_before_each_delete_batch():
    class Orders(Collection):
        def __init__(self):
            super().__init__(docs=[])
            self.calls = 0

        def find(self, query, projection):
            self.calls += 1
            docs = [{"data": {"VBELN": "SO-Y"}}] if self.calls == 1 else [{"data": {"VBELN": "SO-X"}}]
            return Cursor(docs)

    orders = Orders()
    repairs = Collection([{"_id": "r1", "VBELN": "SO-X"}])
    result = cleaner.clean_records(
        DB(orders, repairs), "repairs", "orders", "runs", apply=True,
        batch_size=1, limit=None, source_view="ZSGV_ZZT_WLJL", all_source_views=False,
        from_date=None, to_date=None, run_id="run-recheck",
    )
    assert result["orphan_records"] == 1
    assert result["deleted"] == 0
    assert repairs.docs == [{"_id": "r1", "VBELN": "SO-X"}]


def test_all_source_views_and_empty_order_guard():
    orders = Collection(docs=[])
    with pytest.raises(RuntimeError, match="没有有效 VBELN"):
        cleaner.clean_records(
            DB(orders, Collection()), "repairs", "orders", "runs", apply=False,
            batch_size=10, limit=None, source_view="ZSGV_ZZT_WLJL", all_source_views=True,
            from_date=None, to_date=None, run_id="run-3",
        )
    assert cleaner.build_filter("ZSGV_ZZT_WLJL", True, None, None) == {}


def test_failed_delete_exposes_completed_batch_progress():
    class FailingCollection(Collection):
        def delete_many(self, query):
            if self.deleted_filters:
                raise RuntimeError("temporary delete failure")
            return super().delete_many(query)

    repairs = FailingCollection([
        {"_id": "r1", "VBELN": "SO-X"},
        {"_id": "r2", "VBELN": "SO-Y"},
    ])
    with pytest.raises(cleaner.CleanupError) as error:
        cleaner.clean_records(
            DB(Collection(docs=[{"data": {"VBELN": "SO-1"}}]), repairs), "repairs", "orders", "runs", apply=True,
            batch_size=1, limit=None, source_view="ZSGV_ZZT_WLJL", all_source_views=False,
            from_date=None, to_date=None, run_id="run-4",
        )
    assert error.value.stats["deleted"] == 1
    assert error.value.stats["batches"] == 2


def test_parser_requires_confirmation_for_apply():
    parser = cleaner.build_parser()
    args = parser.parse_args(["--apply"])
    with pytest.raises(RuntimeError, match="--confirm"):
        cleaner.run(args)
