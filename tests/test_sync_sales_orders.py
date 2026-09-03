from datetime import date, datetime, timezone
from decimal import Decimal
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts.sync import sync_sales_orders as sync


def test_parse_date_and_prodh():
    assert sync.parse_date("2026-01-02") == date(2026, 1, 2)
    with pytest.raises(Exception):
        sync.parse_date("2026/01/02")
    assert sync.parse_prodh("00100， 00200,00300") == ["00100", "00200", "00300"]


def test_payload_and_windows():
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    assert sync.payload(start, end, ["00100"]) == {"CHDAT": "2026-01-01", "CHDAT_TO": "2026-01-10", "PRODH_LIST": ["00100"]}
    assert "PRODH_LIST" not in sync.payload(start, end, None)
    assert sync.date_windows(start, end, 7) == [(date(2026, 1, 1), date(2026, 1, 7)), (date(2026, 1, 8), end)]


def test_dotenv_uri_and_configuration(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TEST_VALUE='from-file'\n# ignored\n", encoding="utf-8")
    monkeypatch.delenv("TEST_VALUE", raising=False)
    sync.load_dotenv(dotenv)
    assert sync.env("TEST_VALUE") == "from-file"
    monkeypatch.setenv("MONGODB_HOSTS", "db1:27017, db2:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "fault db")
    monkeypatch.setenv("MONGODB_USERNAME", "user@x")
    monkeypatch.setenv("MONGODB_PASSWORD", "p/a")
    monkeypatch.setenv("MONGODB_AUTH_SOURCE", "admin")
    uri = sync.mongo_uri()
    assert "user%40x:p%2Fa@db1:27017,db2:27017" in uri and "authSource=admin" in uri
    args = SimpleNamespace(all_prodh=True, prodh_list=None)
    assert sync.configured_prodh(args) is None
    args = SimpleNamespace(all_prodh=False, prodh_list=["00100"])
    assert sync.configured_prodh(args) == ["00100"]


def test_save_and_read_watermark(monkeypatch):
    db = FakeDB()
    sync.save_watermark(db, "checkpoints", "SG", date(2026, 1, 2), "run-1")
    assert sync.read_watermark(db, "checkpoints", "SG") == date(2026, 1, 2)
    db["checkpoints"].docs[0]["last_success_date"] = "bad"
    assert sync.read_watermark(db, "checkpoints", "SG") is None


@pytest.mark.parametrize("value, expected", [(None, 0.0), (True, 0.0), ("1,234.50", 1234.5), ("bad", 0.0), (2, 2.0)])
def test_number(value, expected):
    assert sync.number(value) == expected


def test_bson_value_and_keys():
    converted = sync.bson_value({"when": date(2026, 1, 1), "nested": [Decimal("1.2")]})
    assert isinstance(converted["when"], datetime)
    assert str(converted["nested"][0]) == "1.2"
    row = {"A": " x ", "B": "y"}
    assert sync.source_key(row, ("A", "B")) == '["x","y"]'
    assert sync.source_key({"A": ""}, ("A", "B")).startswith("full_row:")


def test_validate_records():
    assert sync.validate_records({"MSGTY": "S", "DATA": [{"AUFNR": "1"}]}, "SG") == [{"AUFNR": "1"}]
    for body in ({"MSGTY": "E", "MSGTX": "bad"}, [], {"DATA": ["not a row"]}, {}):
        with pytest.raises(RuntimeError):
            sync.validate_records(body, "SG")


def test_watermark_and_aggregate():
    current = {"ZDATE": "20260101", "ZTIME": "120000"}
    assert sync.watermark_max(current, {"ZDATE": "20260101", "ZTIME": "110000"}, ("ZDATE", "ZTIME")) == current
    newer = sync.watermark_max(current, {"ZDATE": "20260102", "ZTIME": "000000"}, ("ZDATE", "ZTIME"))
    assert newer == {"ZDATE": "20260102", "ZTIME": "000000"}
    assert sync.watermark_max(newer, {"ZDATE": "", "ZTIME": ""}, ("ZDATE", "ZTIME")) == newer

    rows = [
        {"AUFNR": "100", "GAMNG": "1,000", "WMENG": "2", "VBELN": "SO1"},
        {"AUFNR": "100", "GAMNG": "2", "WMENG": "3", "VBELN": "SO1"},
        {"AUFNR": "", "GAMNG": "9"},
    ]
    docs, stats = sync.aggregate("SG", rows, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert stats == {"fetched": 3, "unique": 1, "skipped": 1}
    assert docs[0]["_id"] == "SG:100" and docs[0]["record_count"] == 2
    assert docs[0]["order_quantity"] == 1002 and docs[0]["storage_quantity"] == 5
    assert sync.aggregate("KK", rows, datetime.now(timezone.utc))[0][0]["_id"] == "KK:100"


def test_repair_query_modes():
    sql, params = sync.repair_query("full", None, date(2026, 1, 1), date(2026, 1, 31))
    assert '"ZDATE" >= ?' in sql and '"ZDATE" <= ?' in sql
    assert params == ["20260101", "20260131"]
    checkpoint = {"watermark": {"ZDATE": "20260115", "ZTIME": "101010"}}
    _, params = sync.repair_query("incremental", checkpoint, date(2026, 1, 8), date(2026, 1, 31))
    assert params[:3] == ["20260115", "20260115", "101010"]


def test_repair_sync_start_date_is_hard_limited():
    assert sync.REPAIR_START_DATE == date(2026, 1, 1)
    sql, params = sync.repair_query("full", None, sync.REPAIR_START_DATE, date(2026, 1, 31))
    assert '"ZDATE" >= ?' in sql
    assert params[0] == "20260101"


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.indexes = []
        self.last_update = None

    def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    def find_one(self, query, *_args, **_kwargs):
        key = query.get("_id")
        return next((doc for doc in self.docs if doc.get("_id") == key), None)

    def update_one(self, query, update, upsert=False):
        key = query.get("_id")
        doc = next((item for item in self.docs if item.get("_id") == key), None)
        if doc is None and upsert:
            doc = {"_id": key}
            self.docs.append(doc)
        if doc is not None:
            doc.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=1 if doc else 0, upserted_id=key if doc and len(doc) == 1 else None)

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def delete_many(self, query):
        sources = set(query.get("source", {}).get("$in", []))
        scope = query.get("_scope_run_id", {}).get("$ne")
        before = len(self.docs)
        self.docs = [doc for doc in self.docs if not (doc.get("source") in sources and doc.get("_scope_run_id") != scope)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def find(self, *_args, **_kwargs):
        return list(self.docs)

    def distinct(self, field):
        values = []
        for doc in self.docs:
            value = doc
            for part in field.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if value is not None:
                values.append(value)
        return values

    def bulk_write(self, operations, ordered=False):
        for operation in operations:
            update = operation._doc
            key_field, key = next(iter(operation._filter.items()))
            found = next((doc for doc in self.docs if doc.get(key_field) == key), None)
            if found is None:
                found = {key_field: key}
                self.docs.append(found)
                found.update(update.get("$setOnInsert", {}))
            found.update(update.get("$set", {}))
            for field, value in update.get("$inc", {}).items():
                found[field] = found.get(field, 0) + value
        return SimpleNamespace(upserted_count=0, modified_count=len(operations))


class FakeDB:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())


class FakeMongoClient:
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.db = FakeDB()
        self.closed = False
        self.__class__.instances.append(self)

    def __getitem__(self, _name):
        return self.db

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, body, error=None):
        self.body, self.error = body, error

    def raise_for_status(self):
        if self.error:
            raise RuntimeError(self.error)

    def json(self):
        return self.body


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url].pop(0)


def test_upsert_is_idempotent_shape(monkeypatch):
    db = FakeDB()
    now = datetime.now(timezone.utc)
    docs = [{"_id": "SG:1", "source": "SG", "aufnr": "1"}]
    inserted, updated = sync.upsert(db, "orders", docs, now)
    assert (inserted, updated) == (1, 0)
    inserted, updated = sync.upsert(db, "orders", docs, now)
    assert (inserted, updated) == (0, 1)
    assert db["orders"].docs[0]["first_seen_at"] == now
    assert db["orders"].docs[0]["sync_count"] == 2


def test_sync_sales_full_run_advances_only_successful_sources(monkeypatch):
    FakeMongoClient.instances.clear()
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    monkeypatch.setenv("SYNC_CHECKPOINT_COLLECTION", "checkpoints")
    monkeypatch.setenv("SYNC_RUN_COLLECTION", "runs")
    monkeypatch.setenv("MONGODB_HOSTS", "localhost:27017")
    monkeypatch.setattr(sync, "MongoClient", FakeMongoClient)
    monkeypatch.setattr(sync, "SOURCES", {"SG": "http://sg", "KK": "http://kk"})
    http = FakeHTTPClient({
        "http://sg": [FakeResponse({"MSGTY": "S", "DATA": [{"AUFNR": "1", "GAMNG": "2", "WMENG": "1"}]} )],
        "http://kk": [FakeResponse({}, "KK unavailable")],
    })
    monkeypatch.setattr(sync.httpx, "Client", lambda **_kwargs: http)
    args = SimpleNamespace(full=True, start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), lookback_days=0, dry_run=False, all_prodh=True, prodh_list=None)
    result = sync.sync_sales(args)
    assert result["success"] is False and result["sources"]["SG"]["success"] is True
    assert result["sources"]["KK"]["success"] is False
    db = FakeMongoClient.instances[0].db
    assert db["orders"].docs[0]["_id"] == "SG:1"
    assert "deleted_out_of_scope" not in result
    assert db["checkpoints"].find_one({"_id": "sales_orders:SG"})["last_success_date"] == "2026-01-02"
    assert db["checkpoints"].find_one({"_id": "sales_orders:KK"}) is None


def test_sync_sales_dry_run_does_not_write(monkeypatch):
    FakeMongoClient.instances.clear()
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("MONGODB_HOSTS", "localhost:27017")
    monkeypatch.setattr(sync, "MongoClient", FakeMongoClient)
    monkeypatch.setattr(sync, "SOURCES", {"SG": "http://sg", "KK": "http://kk"})
    http = FakeHTTPClient({"http://sg": [FakeResponse({"DATA": [{"AUFNR": "1"}]})], "http://kk": [FakeResponse({"DATA": []})]})
    monkeypatch.setattr(sync.httpx, "Client", lambda **_kwargs: http)
    args = SimpleNamespace(full=True, start_date=date(2026, 1, 1), end_date=date(2026, 1, 1), lookback_days=0, dry_run=True, all_prodh=False, prodh_list=None)
    result = sync.sync_sales(args)
    assert result["success"] is True
    db = FakeMongoClient.instances[0].db
    assert db.collections == {}


def test_sync_sales_full_cleans_old_source_documents_after_success(monkeypatch):
    FakeMongoClient.instances.clear()
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    monkeypatch.setenv("MONGODB_HOSTS", "localhost:27017")
    monkeypatch.setattr(sync, "MongoClient", FakeMongoClient)
    monkeypatch.setattr(sync, "SOURCES", {"SG": "http://sg", "KK": "http://kk"})
    http = FakeHTTPClient({
        "http://sg": [FakeResponse({"DATA": [{"AUFNR": "new-sg"}]})],
        "http://kk": [FakeResponse({"DATA": [{"AUFNR": "new-kk"}]})],
    })
    monkeypatch.setattr(sync.httpx, "Client", lambda **_kwargs: http)
    args = SimpleNamespace(full=True, start_date=date(2026, 1, 1), end_date=date(2026, 1, 1), lookback_days=0, dry_run=False, all_prodh=False, prodh_list=None)
    # Seed a stale order after the client is created by sync_sales via a patched constructor.
    original_client = sync.MongoClient
    def seeded_client(*args, **kwargs):
        client = original_client(*args, **kwargs)
        client.db["orders"].docs.append({"_id": "SG:stale", "source": "SG", "_scope_run_id": "old"})
        return client
    monkeypatch.setattr(sync, "MongoClient", seeded_client)
    result = sync.sync_sales(args)
    assert result["success"] is True and result["deleted_out_of_scope"] == 1
    assert {doc["_id"] for doc in FakeMongoClient.instances[-1].db["orders"].docs} == {"SG:new-sg", "KK:new-kk"}


def test_sync_repair_filters_sales_orders_retains_empty_and_advances_watermark(monkeypatch):
    db = FakeDB()
    db["orders"].docs = [{"_id": "SG:1", "data": {"VBELN": "SO-1"}}]
    monkeypatch.setenv("TARGET_COLLECTION", "orders")

    values_by_row = []
    for vbeln, ztime in [("SO-1", "010000"), ("SO-MISSING", "020000"), ("", "030000")]:
        row = {field: "" for field in sync.REPAIR_COLUMNS}
        row.update({"MANDT": "800", "PCODE": "PC-1", "ZMCOD1": "SN-1", "ZDATE_WX": "20260102", "ZDATE": "20260102", "ZTIME": ztime, "VBELN": vbeln})
        values_by_row.append(tuple(row[field] for field in sync.REPAIR_COLUMNS))

    class Cursor:
        def execute(self, _sql, _params):
            pass

        def fetchmany(self, _size):
            if values_by_row:
                return [values_by_row.pop(0)]
            return []

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    @contextmanager
    def fake_hana_connection():
        yield Connection()

    monkeypatch.setattr(sync, "hana_connection", fake_hana_connection)
    result = sync.sync_repair(db, "repairs", "checkpoints", "incremental", date(2026, 1, 1), date(2026, 1, 2), 7, 1, False, "run-1")
    assert result["success"] is True
    assert result["source_rows"] == 3 and result["matched_sales_orders"] == 1 and result["empty_sales_orders_retained"] == 1 and result["filtered_missing_sales_order"] == 1
    assert len(db["repairs"].docs) == 2
    checkpoint = db["checkpoints"].find_one({"_id": "repair_records"})
    assert checkpoint["watermark"] == {"ZDATE": "20260102", "ZTIME": "030000"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"lookback_days": -1},
        {"full": True, "start_date": None},
        {"full": False, "start_date": date(2026, 1, 1)},
        {"full": True, "start_date": date(2026, 1, 3), "end_date": date(2026, 1, 1)},
    ],
)
def test_sync_sales_rejects_invalid_arguments(monkeypatch, overrides):
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    args = SimpleNamespace(full=False, start_date=None, end_date=date(2026, 1, 2), lookback_days=0, dry_run=True, all_prodh=False, prodh_list=None)
    for key, value in overrides.items():
        setattr(args, key, value)
    with pytest.raises((ValueError, RuntimeError)):
        sync.sync_sales(args)


def test_process_lock_blocks_and_releases(tmp_path):
    lock_path = tmp_path / "sync.lock"
    with sync.process_lock(str(lock_path)):
        with pytest.raises(RuntimeError):
            with sync.process_lock(str(lock_path)):
                pass
    with sync.process_lock(str(lock_path)):
        pass
