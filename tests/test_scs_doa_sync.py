from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from scripts.sources import scs_doa_sync as doa
from scripts.sources.scs_doa_sync import (
    backfill_5000_company,
    classify_doa_document,
    extract_equipment,
    parse_datetime,
    parse_labels,
    parse_list,
    parse_pml_context,
    query_window,
    request,
    should_refresh_details,
    source_key,
    build_parser,
)


def test_parse_list_table():
    source = """<form id='a8a80e69292b8364e0192bd28d8220c1c'><table>
      <tr><th name='doa_code'>申报单号</th><th name='service_uid'>服务单号</th></tr>
      <tr value='ctx-1'><td><a href=\"javascript:contextValue=ctx-1\">KX-1</a></td><td>SH-1</td></tr>
    </table></form>"""
    rows = parse_list(source)
    assert rows == [{"doa_code": "KX-1", "service_uid": "SH-1", "_context_value": "ctx-1", "_detail_href": "javascript:contextValue=ctx-1"}]


def test_parse_service_equipment_and_product_fields():
    service = """<form id='a8a80e692922312b1019226d5b47200b5'><table>
      <tr><th name='mtr_sn'>物料SN</th><th name='host_model'>主机型号</th><th name='leave_factory_date'>出厂日期</th><th name='warranty_deadline'>保修结束日期</th></tr>
      <tr><td>SN1</td><td>R5250H0</td><td>2026-08-24</td><td>2029-09-23</td></tr>
    </table></form>contextKey=so_sub_service_order&amp;contextValue=ctx-2"""
    equipment = extract_equipment(service)
    assert equipment[0]["material_sn"] == "SN1"
    assert equipment[0]["host_model"] == "R5250H0"
    assert equipment[0]["leave_factory_date"] == "2026-08-24"
    assert equipment[0]["warranty_deadline"] == "2029-09-23"
    product = parse_labels("<table><tr><th>销售订单:</th><td>XHG2609190</td></tr><tr><th>质保到期日:</th><td>2029-09-23 00:00</td></tr></table>")
    assert product["销售订单"] == "XHG2609190"
    assert product["质保到期日"] == "2029-09-23 00:00"


def test_detail_context_and_stable_key():
    detail = "<a href=\"javascript:loadPml({'pml':'PM_so_service_order_control_main','paras':'contextValue=svc1'})\">SH-1</a>"
    assert parse_pml_context(detail, "PM_so_service_order_control_main") == "svc1"
    assert source_key({"doa_code": " KX-1 "}) == "KX-1"


def test_request_waits_within_default_range(monkeypatch):
    waits = []
    monkeypatch.setattr("scripts.sources.scs_doa_sync.random.uniform", lambda minimum, maximum: (waits.append((minimum, maximum)) or 4.5))
    monkeypatch.setattr("scripts.sources.scs_doa_sync.time.sleep", lambda seconds: waits.append(seconds))

    class Client:
        def request(self, method, url, **kwargs):
            return (method, url, kwargs)

    assert request(Client(), "GET", "/health") == ("GET", "/health", {})
    assert waits == [(3.0, 5.0), 4.5]


@pytest.mark.parametrize(("sales_order", "expected"), [
    ("XNC2519570", True),
    ("XNC251957", True),
    (" xnc2519580 ", True),
    ("XHG2609190", False),
    ("PO-5000", False),
    ("PREFIX-XNC2519570", False),
    ("", False),
    (None, False),
])
def test_classify_doa_document_uses_sales_order_prefix(sales_order, expected):
    derived = classify_doa_document({"sales_order": sales_order})
    assert derived == {
        "sales_order": str(sales_order or "").strip(),
        "is_5000_company": expected,
    }


def test_classify_doa_document_prefers_detail_order_and_falls_back_to_stored_order():
    assert classify_doa_document({"sales_order": "OLD", "details": {"fields": {"合同/订单号": "XNC2519570"}}}) == {
        "sales_order": "XNC2519570", "is_5000_company": True,
    }
    assert classify_doa_document({"sales_order": "XNC1", "details": {"fields": {"订单号": "XHG1"}}})["is_5000_company"] is False
    assert classify_doa_document({"sales_order": "XNC1", "details": {"fields": {"订单号": ""}}})["is_5000_company"] is True


def test_backfill_5000_company_re_evaluates_existing_documents():
    class Collection:
        def __init__(self):
            self.documents = [
                {"_id": "1", "sales_order": "OLD", "details": {"fields": {"合同/订单号": "XNC1"}}, "is_5000_company": False},
                {"_id": "2", "sales_order": "XHG1", "is_5000_company": True},
                {"_id": "3", "sales_order": "XNC2", "is_5000_company": True},
            ]
            self.operations = []

        def find(self, *_args, **_kwargs):
            return self.documents

        def bulk_write(self, operations, ordered=False):
            self.operations.extend(operations)
            return type("Result", (), {"modified_count": len(operations)})()

    class Database:
        def __init__(self): self.collection = Collection()
        def __getitem__(self, _name): return self.collection

    database = Database()
    assert backfill_5000_company(database, "scs_doa_records") == 2
    operations = database.collection.operations
    assert operations[0]._doc["$set"] == {"sales_order": "XNC1", "is_5000_company": True}
    assert operations[1]._doc["$set"] == {"sales_order": "XHG1", "is_5000_company": False}


@pytest.mark.parametrize(("existing", "detail_order", "expected"), [
    (None, "XNC260001", True),
    ({"sales_order": "XNC-old"}, "XHG260001", False),
    ({"sales_order": "XNC260001"}, None, True),
])
def test_sync_classifies_without_reference_tables_and_preserves_order_on_detail_failure(
        monkeypatch, existing, detail_order, expected):
    collection, checkpoints, client = Mock(), Mock(), Mock()
    database = {"doa": collection, "sync_checkpoints": checkpoints}
    client.__getitem__ = Mock(return_value=database)
    checkpoints.find_one.return_value = {"watermark": "2026-10-15"}
    collection.find_one.return_value = existing
    pending = MagicMock()
    pending.sort.return_value = pending
    pending.limit.return_value = pending
    collection.find.side_effect = [pending, []]
    written = []

    def bulk_write(operations, **_kwargs):
        written.extend(operations)
        return SimpleNamespace(upserted_count=1, modified_count=0)

    collection.bulk_write.side_effect = bulk_write
    monkeypatch.setenv("SCS_DOA_COLLECTION", "doa")
    monkeypatch.setenv("MONGODB_DATABASE", "test")
    monkeypatch.setattr(doa, "load_dotenv", lambda *_args: None)
    monkeypatch.setattr(doa, "mongo_uri", lambda: "mongodb://test")
    monkeypatch.setattr(doa, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(doa, "resource_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(doa, "login", lambda *_args: None)
    monkeypatch.setattr(doa, "iter_list", lambda *_args: [[{
        "doa_code": "DOA-1", "declare_time": "2026-10-15 08:00:00",
    }]])
    detail = Mock(return_value={"fields": {"销售订单": detail_order}})
    if detail_order is None:
        detail.side_effect = RuntimeError("detail unavailable")
    monkeypatch.setattr(doa, "fetch_detail", detail)

    result = doa.sync(SimpleNamespace(
        full=False, dry_run=False, page_size=500, lookback_days=2, end_date="2026-10-15",
    ))
    assert result["success"] is True
    assert len(written) == 1
    document = written[0]._doc["$set"]
    assert document["is_5000_company"] is expected
    assert document["sales_order"] == (detail_order or existing["sales_order"])
    assert document["_detail_pending"] is (detail_order is None)
    client.close.assert_called_once()


def test_parse_datetime_accepts_iso_watermark():
    parsed = parse_datetime("2026-09-17T18:44:00+00:00")
    assert parsed == datetime(2026, 9, 17, 18, 44, tzinfo=timezone.utc)
    assert parse_datetime("2026-09-17 18:44") == datetime(2026, 9, 17, 18, 44, tzinfo=timezone.utc)
    assert parse_datetime("") is None


def test_incremental_query_window_uses_watermark_lookback():
    args = SimpleNamespace(full=False, start_date="2026-01-01", lookback_days=2)
    start, end = query_window(args, {"watermark": "2026-09-17T18:44:00+00:00"})
    assert start == "2026-09-15" and end == ""
    fallback = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    assert query_window(args, {"watermark": "unreadable"}) == (fallback, "")
    assert query_window(args, None) == (fallback, "")
    assert query_window(SimpleNamespace(full=True, start_date="2026-01-01", lookback_days=2), None) == ("2026-01-01", "")


def test_parser_defaults_lookback_to_two_days(monkeypatch):
    monkeypatch.delenv("SCS_LOOKBACK_DAYS", raising=False)
    args = build_parser().parse_args([])
    assert args.lookback_days == 2
    monkeypatch.setenv("SCS_LOOKBACK_DAYS", "5")
    args = build_parser().parse_args([])
    assert args.lookback_days == 5


def test_should_refresh_details_skips_unchanged_complete_rows():
    row = {"doa_code": "KX-1", "status": "已受理", "declare_time": "2026-09-17 18:44"}
    existing = {"doa_code": "KX-1", "status": "已受理", "declare_time": "2026-09-17 18:44", "details": {"fields": {"销售订单": "X"}}}
    assert should_refresh_details(existing, row) is False
    assert should_refresh_details(None, row) is True
    assert should_refresh_details({**existing, "status": "已完成"}, row) is True
    assert should_refresh_details({**existing, "details": {"error": "timeout"}}, row) is True
