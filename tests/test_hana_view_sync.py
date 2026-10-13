from types import SimpleNamespace

from pymongo.errors import OperationFailure

from scripts.sources.hana import hana_view_sync as sync


def test_documented_view_specs_are_registered():
    assert set(sync.VIEW_SPECS) == {"ZSGV_ZSD124", "ZSGV_ZPP_SERNOLIST", "Z_V_ZMES_T_001", "ZSGV_ZSD46N_SO_KK"}
    assert sync.VIEW_SPECS["ZSGV_ZSD124"].collection == "order_bom_postings_sap"
    assert sync.VIEW_SPECS["ZSGV_ZPP_SERNOLIST"].watermark_fields == ()
    assert sync.VIEW_SPECS["Z_V_ZMES_T_001"].start_date_field == "ACTUAL_START_TIME"
    station_columns = sync.VIEW_SPECS["Z_V_ZMES_T_001"].columns
    assert station_columns[-4:] == ("HADE7", "HADE8", "STATU", "MESS")
    delivery = sync.VIEW_SPECS["ZSGV_ZSD46N_SO_KK"]
    assert delivery.qualified_name == '"_SYS_BIC"."BW_LOCAL.SD/ZSGV_ZSD46N_SO_KK"'
    assert delivery.collection == "sales_order_delivery_records_sap"
    assert len(delivery.columns) == 36
    assert delivery.key_fields == ("VBELN", "POSNR", "VBELN_L", "POSNR_L", "SERNR", "DATUM")
    assert delivery.fallback_to_full_row


def test_queries_use_documented_watermarks():
    spec = sync.VIEW_SPECS["ZSGV_ZSD124"]
    sql, params = sync.build_query(spec, {"watermark": "20260830"}, "incremental", "20260101", 7)
    assert '"BUDAT_MKPF" >= ?' in sql
    assert params == ["20260823", "20260101"]

    spec = sync.VIEW_SPECS["Z_V_ZMES_T_001"]
    sql, params = sync.build_query(spec, None, "full", "20260101", 7)
    assert '"ACTUAL_START_TIME" >= ?' in sql
    assert params == ["2026-01-01 00:00:00"]

    spec = sync.VIEW_SPECS["ZSGV_ZSD46N_SO_KK"]
    sql, params = sync.build_query(spec, None, "full", "20260101", 7, "20261231")
    assert '"ERDAT" >= ?' in sql and '"ERDAT" <= ?' in sql
    assert params == ["20260101", "20261231"]
    sql, params = sync.build_query(spec, {"watermark": "20260115"}, "incremental", "20260101", 7, "20261231")
    assert '"ERDAT" >= ?' in sql and '"ERDAT" <= ?' in sql
    assert params == ["20260108", "20260101", "20261231"]


def test_undated_view_uses_stable_full_row_key_when_business_key_missing():
    spec = sync.VIEW_SPECS["ZSGV_ZPP_SERNOLIST"]
    row = {field: None for field in spec.columns}
    key = sync.source_key(row, spec)
    assert key.startswith("full_row:")


def test_delivery_source_key_uses_serial_and_hashes_incomplete_keys():
    spec = sync.VIEW_SPECS["ZSGV_ZSD46N_SO_KK"]
    row = {field: "x" for field in spec.columns}
    row.update({"VBELN": "SO-1", "POSNR": "10", "VBELN_L": "DL-1", "POSNR_L": "10", "SERNR": "SN-1"})
    first = sync.source_key(row, spec)
    row["DATUM"] = "20260101"
    first = sync.source_key(row, spec)
    row["DATUM"] = "20260102"
    assert sync.source_key(row, spec) != first
    row["SERNR"] = "SN-2"
    assert sync.source_key(row, spec) != first
    row["SERNR"] = None
    assert sync.source_key(row, spec).startswith("full_row:")


def test_mongo_write_concern_options_are_configurable(monkeypatch):
    monkeypatch.setenv("MONGODB_WRITE_CONCERN_W", "1")
    monkeypatch.setenv("MONGODB_JOURNALED", "false")
    monkeypatch.setenv("MONGODB_WRITE_TIMEOUT_MS", "30000")
    options = sync.mongo_client_options()
    assert options["w"] == 1
    assert options["journal"] is False
    assert options["wTimeoutMS"] == 30000


def test_ensure_index_reuses_legacy_name_for_same_key_pattern():
    class Collection:
        def create_index(self, *_args, **_kwargs):
            raise OperationFailure("Index already exists with a different name", code=85)

        def list_indexes(self):
            return iter([{"name": "SERNR_1", "key": {"SERNR": 1}}])

    assert sync.ensure_index(Collection(), "SERNR", name="source_sernr") == "SERNR_1"


def test_stream_nonempty_field_values_uses_cursor_not_distinct():
    class Cursor:
        closed = False

        def __iter__(self):
            return iter([
                {"data": {"VBELN": " SO-1 "}},
                {"data": {"VBELN": None}},
                {"data": {"VBELN": "SO-2"}},
            ])

        def close(self):
            self.closed = True

    class Collection:
        def __init__(self):
            self.cursor = Cursor()

        def find(self, query, projection):
            assert query == {} and projection == {"data.VBELN": 1}
            return self.cursor

        def distinct(self, *_args):
            raise AssertionError("distinct must not be used")

    collection = Collection()
    assert sync.stream_nonempty_field_values(collection, "data.VBELN") == {"SO-1", "SO-2"}
    assert collection.cursor.closed is True


class FakeCursor:
    def __init__(self):
        self.closed = False
        self.executed = None
        self.batches = [[("1", "2", "3", "4", "00100")], []]

    def execute(self, sql, params):
        self.executed = (sql, params)

    def fetchmany(self, _size):
        return self.batches.pop(0)

    def close(self):
        self.closed = True


class FakeHanaConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class FakeCollection:
    def __init__(self):
        self.documents = []
        self.updated = []
        self.delete_queries = []

    def create_index(self, *_args, **_kwargs):
        return None

    def find_one(self, *_args, **_kwargs):
        return None

    def insert_one(self, document):
        self.documents.append(document)

    def bulk_write(self, operations, ordered=False):
        assert ordered is False
        return SimpleNamespace(upserted_count=1, matched_count=0, modified_count=0)

    def update_one(self, query, update, upsert=False):
        self.updated.append((query, update, upsert))

    def delete_many(self, query):
        self.delete_queries.append(query)
        return SimpleNamespace(deleted_count=0)

    def delete_one(self, query):
        return SimpleNamespace(deleted_count=0)


class FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())


class FakeMongoClient:
    instance = None

    def __init__(self, *_args, **_kwargs):
        self.database = FakeDatabase()
        self.closed = False
        type(self).instance = self

    def __getitem__(self, _name):
        return self.database

    def close(self):
        self.closed = True


def test_sync_view_closes_one_hana_session_cursor_and_mongo_client(monkeypatch):
    connection = FakeHanaConnection()
    monkeypatch.setattr(sync, "MongoClient", FakeMongoClient)
    monkeypatch.setitem(
        __import__("sys").modules,
        "hdbcli",
        SimpleNamespace(dbapi=SimpleNamespace(connect=lambda **_kwargs: connection)),
    )
    monkeypatch.setenv("MONGODB_HOSTS", "localhost:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("HANA_ADDRESS", "localhost")
    monkeypatch.setenv("HANA_USER", "user")
    monkeypatch.setenv("HANA_PASSWORD", "password")
    result = sync.sync_view(
        sync.VIEW_SPECS["ZSGV_ZPP_SERNOLIST"],
        mode="incremental",
        start_date=None,
        batch_size=10,
        lookback_days=7,
        dry_run=False,
    )
    assert result["success"] is True
    assert connection.closed is True
    assert connection.cursor_instance.closed is True
    assert FakeMongoClient.instance.closed is True


def test_sync_view_dry_run_reports_duplicate_source_keys(monkeypatch):
    row = ["x"] * len(sync.VIEW_SPECS["ZSGV_ZSD46N_SO_KK"].columns)
    connection = FakeHanaConnection()
    connection.cursor_instance.batches = [[tuple(row), tuple(row)], []]
    monkeypatch.setitem(__import__("sys").modules, "hdbcli", SimpleNamespace(dbapi=SimpleNamespace(connect=lambda **_kwargs: connection)))
    monkeypatch.setenv("HANA_ADDRESS", "localhost")
    monkeypatch.setenv("HANA_USER", "user")
    monkeypatch.setenv("HANA_PASSWORD", "password")
    result = sync.sync_view(
        sync.VIEW_SPECS["ZSGV_ZSD46N_SO_KK"],
        mode="full",
        start_date="20260101",
        end_date="20261231",
        batch_size=10,
        lookback_days=7,
        dry_run=True,
        validate_source_keys=True,
    )
    assert result["success"] is False
    assert result["source_rows"] == 2
    assert result["duplicate_source_keys"] == 1
    assert connection.closed is True
    assert connection.cursor_instance.closed is True


def test_full_sync_defers_old_row_deletion_until_cleanup_commits(monkeypatch):
    connection = FakeHanaConnection()
    monkeypatch.setattr(sync, "MongoClient", FakeMongoClient)
    monkeypatch.setitem(__import__("sys").modules, "hdbcli", SimpleNamespace(dbapi=SimpleNamespace(connect=lambda **_kwargs: connection)))
    monkeypatch.setenv("MONGODB_HOSTS", "localhost:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setenv("HANA_ADDRESS", "localhost")
    monkeypatch.setenv("HANA_USER", "user")
    monkeypatch.setenv("HANA_PASSWORD", "password")
    spec = sync.VIEW_SPECS["ZSGV_ZPP_SERNOLIST"]
    result = sync.sync_view(spec, mode="full", start_date=None, batch_size=10, lookback_days=7, dry_run=False, defer_finalize=True)
    assert result["success"] is True and result["finalized"] is False
    assert FakeMongoClient.instance.database[spec.collection].delete_queries == []
