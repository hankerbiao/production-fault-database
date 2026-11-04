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


@pytest.mark.parametrize(("header", "customer"), [("customer_name", "北京字节跳动网络技术有限公司"), ("customer_uid", "旧客户")])
def test_parse_list_maps_source_customer_to_customer_uid(header, customer):
    source = f"""<form id='a8a80e69292b8364e0192bd28d8220c1c'><table>
      <tr><th name='doa_code'>申报单号</th><th name='{header}'>客户</th></tr>
      <tr><td>KX-1</td><td>{customer}</td></tr></table></form>"""
    assert parse_list(source)[0]["customer_uid"] == customer


def test_parse_service_equipment_and_product_fields():
    service = """<form id='a8a80e692922312b1019226d5b47200b5'><table>
      <tr><th name='mtr_sn'>物料SN</th><th name='host_model'>主机型号</th><th name='leave_factory_date'>出厂日期</th><th name='warranty_deadline'>保修结束日期</th></tr>
      <tr><td><a href="?contextKey=so_sub_service_order&amp;contextValue=ctx-2">SN1</a></td><td>R5250H0</td><td>2026-08-24</td><td>2029-09-23</td></tr>
    </table></form>"""
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
    from test_sync_sales_orders import FakeDB
    database = FakeDB()
    database["doa"].docs = [
                {"_id": "1", "sales_order": "OLD", "details": {"fields": {"合同/订单号": "XNC1"}}, "is_5000_company": False},
                {"_id": "2", "sales_order": "XHG1", "is_5000_company": True},
                {"_id": "3", "sales_order": "XNC2", "is_5000_company": True},
    ]
    for document in database["doa"].docs:
        document.update(_prodh_scope="00100", service_order_keys=[])
    assert backfill_5000_company(database, "doa") == 2
    assert database["doa"].docs[0]["sales_order"] == "XNC1"
    assert database["doa"].docs[0]["is_5000_company"] is True
    assert database["doa"].docs[1]["is_5000_company"] is False


@pytest.mark.parametrize(("existing", "detail_order", "expected"), [
    (None, "XNC260001", True),
    ({"sales_order": "XNC-old", "customer_uid": "原客户"}, "XHG260001", False),
    ({"sales_order": "XNC260001", "customer_uid": "原客户"}, None, True),
])
def test_sync_classifies_verified_orders_and_preserves_order_on_detail_failure(
        monkeypatch, existing, detail_order, expected):
    from test_sync_admission import setup_scs
    from test_sync_sales_orders import FakeDB
    database = FakeDB()
    args = setup_scs(monkeypatch, doa, database)
    args.full = False
    args.end_date = ""
    sales_order = detail_order or existing["sales_order"]
    database["orders"].docs = [{"source": "SG", "_prodh_scope": "00100",
                                "data": {"AUFNR": "1", "VBELN": sales_order}}]
    if existing:
        database["doa"].docs = [{
            "_id": "D1", "_source_key": "DOA-1", "_prodh_scope": "00100", **existing,
            "details": {"fields": {"销售订单": existing["sales_order"]}},
        }]
    monkeypatch.setattr(doa, "iter_list", lambda *_args: [[{
        "doa_code": "DOA-1", "declare_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }]])
    detail = Mock(return_value={"fields": {"销售订单": detail_order, "客户全称": "详情客户"}})
    if detail_order is None:
        detail.side_effect = RuntimeError("detail unavailable")
    monkeypatch.setattr(doa, "fetch_detail", detail)

    result = doa.sync(args)
    assert result["success"] is True
    assert len(database["doa"].docs) == 1
    checkpoint = database["checkpoints"].find_one({"_id": "scs_doa:scs"})
    assert checkpoint is not None and "prodh_list" not in checkpoint
    document = database["doa"].docs[0]
    assert document["is_5000_company"] is expected
    assert document["sales_order"] == (detail_order or existing["sales_order"])
    assert document["customer_uid"] == (existing["customer_uid"] if existing else "详情客户")
    assert document["_detail_pending"] is (detail_order is None)


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
    existing.update(_detail_schema_version=doa.DETAIL_SCHEMA_VERSION, _detail_fetched_at=datetime.now(timezone.utc))
    assert should_refresh_details(existing, row) is False
    assert should_refresh_details(None, row) is True
    assert should_refresh_details({**existing, "status": "已完成"}, row) is True
    assert should_refresh_details({**existing, "details": {"error": "timeout"}}, row) is True
