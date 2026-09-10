from scripts.sources.scs_doa_sync import (
    backfill_5000_company,
    classify_doa_document,
    extract_equipment,
    parse_labels,
    parse_list,
    parse_pml_context,
    request,
    source_key,
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


def test_classify_doa_document_uses_detail_sales_order():
    assert classify_doa_document({"sales_order": "OLD", "details": {"fields": {"合同/订单号": "XHG2609190"}}}, {"XHG2609190"}) == {
        "sales_order": "XHG2609190", "is_5000_company": True,
    }
    assert classify_doa_document({"details": {"fields": {"合同/订单号": "XHG-NOT-FOUND"}}}, {"XHG2609190"})["is_5000_company"] is False


def test_backfill_5000_company_re_evaluates_existing_documents():
    class Collection:
        def __init__(self):
            self.documents = [{"_id": "1", "sales_order": "OLD", "details": {"fields": {"合同/订单号": "XHG1"}}, "is_5000_company": False}]
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
    assert backfill_5000_company(database, "scs_doa_records", {"XHG1"}) == 1
    assert database.collection.operations
