from types import SimpleNamespace

import pytest

from scripts import table_pipeline as pipeline
from scripts.sources.hana.hana_view_sync import VIEW_SPECS


class Collection:
    def __init__(self, docs):
        self.docs = docs
        self.deleted_queries = []

    def find(self, query, _projection=None):
        def matches(row):
            for key, value in query.items():
                if isinstance(value, dict):
                    if "$in" in value and row.get(key) not in value["$in"]:
                        return False
                    if "$ne" in value and row.get(key) == value["$ne"]:
                        return False
                elif row.get(key) != value:
                    return False
            return True
        return iter([dict(row) for row in self.docs if matches(row)])

    def delete_many(self, query):
        self.deleted_queries.append(query)
        ids = set(query["_id"]["$in"])
        before = len(self.docs)
        self.docs[:] = [row for row in self.docs if row["_id"] not in ids]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def count_documents(self, query):
        return len(self.docs)


class DB:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, _name):
        if _name.endswith("_archive"):
            return SimpleNamespace(bulk_write=lambda *args, **kwargs: None)
        return self.collection


def test_serial_cleanup_removes_only_complete_exact_duplicates():
    spec = VIEW_SPECS["ZSGV_ZPP_SERNOLIST"]
    collection = Collection([
        {"_id": "keep", "_source_view": spec.view_id, "_sync_run_id": "run", "ZCODE_HEAD": "A", "ZCODE_ITEM": "B", "AUFNR_HEAD": "1", "AUFNR_ITEM": "2", "PRODH": "3"},
        {"_id": "delete", "_source_view": spec.view_id, "_sync_run_id": "run", "ZCODE_HEAD": "A", "ZCODE_ITEM": "B", "AUFNR_HEAD": "1", "AUFNR_ITEM": "2", "PRODH": "3"},
        {"_id": "incomplete", "_source_view": spec.view_id, "_sync_run_id": "run", "ZCODE_HEAD": "", "ZCODE_ITEM": "B", "AUFNR_HEAD": "1", "AUFNR_ITEM": "2", "PRODH": "3"},
    ])
    result = pipeline.serial_cleanup(DB(collection), spec, apply=True, run_id="run", batch_size=10)
    assert result["duplicate_candidates"] == 1
    assert result["incomplete_business_key_rows"] == 1
    assert result["deleted"] == 1
    assert {row["_id"] for row in collection.docs} == {"keep", "incomplete"}


def test_full_mode_requires_start_date():
    args = SimpleNamespace(mode="full", start_date=None, end_date=pipeline.date.today(), batch_size=1, lookback_days=0)
    with pytest.raises(ValueError, match="start-date"):
        pipeline.validate_args(args)


def test_incremental_serial_cleanup_also_removes_legacy_duplicates():
    spec = VIEW_SPECS["ZSGV_ZPP_SERNOLIST"]
    collection = Collection([
        {"_id": "old", "_source_view": spec.view_id, "_sync_run_id": "old", "ZCODE_HEAD": "A", "ZCODE_ITEM": "B", "AUFNR_HEAD": "1", "AUFNR_ITEM": "2", "PRODH": "3"},
        {"_id": "new", "_source_view": spec.view_id, "_sync_run_id": "new", "ZCODE_HEAD": "A", "ZCODE_ITEM": "B", "AUFNR_HEAD": "1", "AUFNR_ITEM": "2", "PRODH": "3"},
    ])
    result = pipeline.serial_cleanup(DB(collection), spec, apply=True, run_id="new", batch_size=10)
    assert result["scanned"] == 2 and result["deleted"] == 1
    assert {row["_id"] for row in collection.docs} == {"new"}
