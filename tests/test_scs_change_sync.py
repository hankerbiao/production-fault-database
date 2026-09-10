import argparse

from scripts.sources.scs_change_sync import (
    bounded_change_range,
    change_source_key,
    fetch_changes,
    load_5000_service_orders,
    parse_changes,
    request,
    sync,
)


def test_parse_changes_maps_all_result_fields():
    source = """<form id='a8a80e692948867c501948be54ba50b6f'><table>
      <tr><th>服务单号</th><th>客户名称</th><th>报修设备</th><th>操作类型</th><th>PN</th><th>SN</th><th>备件名称</th><th>原厂原码</th><th>备注</th><th>是否需要归还</th><th>操作人</th><th>操作时间</th><th>是否撤销</th><th>撤销人</th><th>撤销时间</th></tr>
      <tr><td>SH-1</td><td>客户</td><td>HOST-1</td><td>换上-up</td><td>33000306</td><td>PART-1</td><td>备件</td><td>ORIG</td><td>备注</td><td>否</td><td>张三</td><td>2026-09-08 22:37:25</td><td>否</td><td></td><td></td></tr>
    </table></form>"""
    rows = parse_changes(source)
    assert rows[0]["so_code"] == "SH-1"
    assert rows[0]["part_number"] == "33000306"
    assert rows[0]["create_time"] == "2026-09-08 22:37:25"
    assert change_source_key(rows[0]) == change_source_key(rows[0])


def test_fetch_changes_paginates(monkeypatch):
    html = "<form id='a8a80e692948867c501948be54ba50b6f'><table><tr><th>服务单号</th></tr><tr><td>SH-1</td></tr></table></form>"
    calls = []

    class Response:
        text = html
        def raise_for_status(self): pass

    def fake_request(client, method, url, **kwargs):
        calls.append(kwargs["params"]["pageNo"])
        return Response() if len(calls) == 1 else type("EmptyResponse", (), {"text": "", "raise_for_status": lambda self: None})()

    monkeypatch.setattr("scripts.sources.scs_change_sync.request", fake_request)
    assert len(fetch_changes(object(), page_size=1)) == 1
    assert calls == [1, 2]


def test_change_queries_are_limited_to_2026():
    assert bounded_change_range() == ("2026-01-01", "2026-12-31")
    assert bounded_change_range("2025-12-01", "2027-01-01") == ("2026-01-01", "2026-12-31")
    assert bounded_change_range("2026-03-01", "2026-04-01") == ("2026-03-01", "2026-04-01")


def test_change_request_uses_five_to_ten_second_random_delay(monkeypatch):
    waits = []
    monkeypatch.setattr("scripts.sources.scs_change_sync.random.uniform", lambda minimum, maximum: (waits.append((minimum, maximum)) or 7))
    monkeypatch.setattr("scripts.sources.scs_change_sync.time.sleep", lambda value: waits.append(value))

    class Client:
        def request(self, method, url, **kwargs):
            return (method, url, kwargs)

    assert request(Client(), "GET", "/change")[0] == "GET"
    assert waits == [(5.0, 10.0), 7]


def test_load_5000_service_orders():
    class Collection:
        def find(self, *_args, **_kwargs):
            return [{"service_uid": "SH-1"}, {"service_uid": "", "details": {"service_fields": {"服务单号": "SH-2"}}}]
    class Database:
        def __getitem__(self, _name): return Collection()
    assert load_5000_service_orders(Database()) == {"SH-1", "SH-2"}


def test_sync_fetches_and_writes_one_page_at_a_time(monkeypatch):
    rows = [
        {"so_code": "SH-1", "create_time": "2026-09-08 10:00:00"},
        {"so_code": "SH-2", "create_time": "2026-09-08 11:00:00"},
        {"so_code": "SH-3", "create_time": "2026-09-08 12:00:00"},
    ]
    pages = {1: rows[:2], 2: rows[2:], 3: []}
    requested_pages = []

    def fake_fetch_page(_client, page, *_args, **_kwargs):
        requested_pages.append(page)
        return pages[page]

    class Result:
        upserted_count = 1
        modified_count = 0

    class Collection:
        def __init__(self):
            self.writes = []

        def create_index(self, *_args, **_kwargs):
            return None

        def bulk_write(self, operations, **_kwargs):
            self.writes.append(list(operations))
            return Result()

        def delete_many(self, *_args, **_kwargs):
            return type("DeleteResult", (), {"deleted_count": 0})()

    class Checkpoints:
        def __init__(self):
            self.updates = []

        def find_one(self, *_args, **_kwargs):
            return None

        def update_one(self, query, update, **_kwargs):
            self.updates.append((query, update))

    class Database:
        def __init__(self):
            self.collection = Collection()
            self.checkpoints = Checkpoints()

        def __getitem__(self, name):
            return self.checkpoints if name == "sync_checkpoints" else self.collection

    database = Database()

    class Mongo:
        def __getitem__(self, _name):
            return database

        def close(self):
            pass

    class HttpClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr("scripts.sources.scs_change_sync.httpx.Client", HttpClient)
    monkeypatch.setattr("scripts.sources.scs_change_sync.MongoClient", lambda *_args, **_kwargs: Mongo())
    monkeypatch.setattr("scripts.sources.scs_change_sync.mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr("scripts.sources.scs_change_sync.login", lambda _client: None)
    monkeypatch.setattr("scripts.sources.scs_change_sync.fetch_change_page", fake_fetch_page)
    monkeypatch.setattr("scripts.sources.scs_change_sync.load_5000_service_orders", lambda *_args: set())
    monkeypatch.setenv("SCS_WRITE_BATCH_SIZE", "2")

    result = sync(argparse.Namespace(full=True, start_date="2026-01-01", lookback_days=7, page_size=2, dry_run=False))

    assert requested_pages == [1, 2]
    assert [len(write) for write in database.collection.writes] == [2, 1]
    checkpoint_sets = [update[1]["$set"] for update in database.checkpoints.updates]
    assert any(item["page"] == 1 and item["page_row_index"] == 2 for item in checkpoint_sets)
    assert checkpoint_sets[-1]["status"] == "completed"
    assert result["fetched"] == 3
