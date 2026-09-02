from datetime import date
from types import SimpleNamespace

from scripts.maintenance import clean_order_boards as cleaner


class Cursor(list):
    def sort(self, *_args):
        return self

    def limit(self, size):
        return Cursor(self[:size])

    def close(self):
        return None


class Collection:
    def __init__(self, docs):
        self.docs = [dict(doc) for doc in docs]
        self.bulk_calls = 0
        self.delete_calls = 0

    def count_documents(self, query):
        return sum(1 for d in self.docs if d.get("_source_view") == query.get("_source_view"))

    def find(self, query=None, projection=None):
        query = query or {}
        if projection and "data.AUFNR" in projection:
            return Cursor(self.docs)
        rows = []
        for doc in self.docs:
            if query.get("_source_view") and doc.get("_source_view") != query["_source_view"]:
                continue
            rows.append({"_id": doc["_id"], **{key: doc.get(key) for key in projection if key != "_id"}})
        return Cursor(rows)

    def bulk_write(self, operations, ordered=False):
        self.bulk_calls += 1
        for operation in operations:
            target = next(d for d in self.docs if d["_id"] == operation._filter["_id"])
            target.update(operation._doc["$set"])
        return SimpleNamespace(modified_count=len(operations))

    def delete_many(self, query):
        self.delete_calls += 1
        ids = {condition["_id"] for condition in query["$or"]}
        before = len(self.docs)
        self.docs = [d for d in self.docs if d["_id"] not in ids]
        return SimpleNamespace(deleted_count=before - len(self.docs))


class DB:
    def __init__(self, bom, station, orders):
        self.collections = {"bom": bom, "station": station, "orders": orders}

    def __getitem__(self, key):
        return self.collections[key]


def test_bom_keeps_only_5000_company_cpx_values():
    view = cleaner.SPECS["bom"].source_view
    bom = Collection([
        {"_id": "keep", "_source_view": view, "CPX": "5000公司", "AUFNR_1": "100", "VBELN_EX": "SO-1"},
        {"_id": "keep-trimmed", "_source_view": view, "CPX": " 5000公司 ", "AUFNR_1": "200", "VBELN_EX": "SO-2"},
        {"_id": "other-company", "_source_view": view, "CPX": "8301公司"},
        {"_id": "empty", "_source_view": view, "CPX": ""},
    ])
    result = cleaner.process_board(
        DB(bom, Collection([]), Collection([])), cleaner.SPECS["bom"], "bom", "orders",
        apply=True, batch_size=10, limit=None, from_date=None, to_date=None, progress=False,
    )
    assert result["cpx_5000_company"] == 2
    assert result["empty_cpx"] == 1
    assert result["non_5000_cpx"] == 2
    assert result["delete_candidates"] == 2 and result["deleted"] == 2
    assert bom.bulk_calls == 0
    assert {d["_id"] for d in bom.docs} == {"keep", "keep-trimmed"}


def test_station_preview_does_not_write_and_keeps_unresolvable_rows():
    view = cleaner.SPECS["station"].source_view
    station = Collection([
        {"_id": "known", "_source_view": view, "AUFNR": "1", "KDAUF": ""},
        {"_id": "empty", "_source_view": view, "AUFNR": "", "KDAUF": ""},
    ])
    orders = Collection([{"data": {"AUFNR": "1", "VBELN": "SO-1"}}])
    result = cleaner.process_board(DB(Collection([]), station, orders), cleaner.SPECS["station"], "station", "orders", apply=False, batch_size=10, limit=None, from_date=None, to_date=None, progress=False)
    assert result["filled_sales_order"] == 1
    assert result["updated"] == 0 and result["deleted"] == 0
    assert station.bulk_calls == 0 and station.docs[0]["KDAUF"] == ""


def test_station_unknown_production_with_empty_sales_is_retained_as_unresolved():
    view = cleaner.SPECS["station"].source_view
    station = Collection([{"_id": "unknown", "_source_view": view, "AUFNR": "999", "KDAUF": ""}])
    orders = Collection([{"data": {"AUFNR": "1", "VBELN": "SO-1"}}])
    result = cleaner.process_board(
        DB(Collection([]), station, orders),
        cleaner.SPECS["station"],
        "station",
        "orders",
        apply=True,
        batch_size=10,
        limit=None,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 1),
        progress=False,
    )
    assert result["unresolved_rows"] == 1
    assert result["unresolved_production_orders"] == 1
    assert result["delete_candidates"] == 0 and result["deleted"] == 0
    assert station.docs[0]["_id"] == "unknown"


def test_station_does_not_fill_ambiguous_production_mapping():
    view = cleaner.SPECS["station"].source_view
    station = Collection([{"_id": "ambiguous", "_source_view": view, "AUFNR": "1", "KDAUF": ""}])
    orders = Collection([
        {"data": {"AUFNR": "1", "VBELN": "SO-1"}},
        {"data": {"AUFNR": "1", "VBELN": "SO-2"}},
    ])
    result = cleaner.process_board(
        DB(Collection([]), station, orders), cleaner.SPECS["station"], "station", "orders",
        apply=True, batch_size=10, limit=None, from_date=None, to_date=None, progress=False,
    )
    assert result["filled_sales_order"] == 0
    assert result["ambiguous_production_mappings"] == 1
    assert result["unresolved_rows"] == 1
    assert station.docs[0]["KDAUF"] == ""


def test_station_deletes_nonempty_sales_order_missing_from_board():
    view = cleaner.SPECS["station"].source_view
    station = Collection([
        {"_id": "known", "_source_view": view, "AUFNR": "1", "KDAUF": "SO-1"},
        {"_id": "orphan", "_source_view": view, "AUFNR": "9", "KDAUF": "SO-X"},
        {"_id": "empty", "_source_view": view, "AUFNR": "", "KDAUF": ""},
    ])
    orders = Collection([{"data": {"AUFNR": "1", "VBELN": "SO-1"}}])
    result = cleaner.process_board(
        DB(Collection([]), station, orders), cleaner.SPECS["station"], "station", "orders",
        apply=True, batch_size=10, limit=None,
        from_date=date(2026, 1, 1), to_date=date(2026, 1, 1), progress=False,
    )
    assert result["delete_candidates"] == 1 and result["deleted"] == 1
    assert {row["_id"] for row in station.docs} == {"known", "empty"}


def test_station_apply_without_date_range_uses_full_dataset():
    view = cleaner.SPECS["station"].source_view
    station = Collection([{"_id": "known", "_source_view": view, "AUFNR": "1", "KDAUF": "SO-1"}])
    orders = Collection([{"data": {"AUFNR": "1", "VBELN": "SO-1"}}])
    result = cleaner.process_board(
        DB(Collection([]), station, orders), cleaner.SPECS["station"], "station", "orders",
        apply=True, batch_size=10, limit=None, from_date=None, to_date=None, progress=False,
    )
    assert result["success"] is True
    assert result["scanned"] == 1
