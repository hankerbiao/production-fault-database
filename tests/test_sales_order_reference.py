from scripts.sales_order_reference import SalesReferences, sales_lines


def test_company_membership_uses_either_order_and_never_component_prodh():
    refs = SalesReferences([
        {"source": "SG", "data": {"AUFNR": "001", "VBELN": "SO-1", "PRODH": "00200"}},
        {"source": "SG", "data": {"AUFNR": "2", "VBELN": "SO-1"}},
    ])
    assert refs.company_scope({"AUFNR": "1", "VBELN": "unknown", "PRODH": "999"}) == "in_scope"
    assert refs.company_scope({"AUFNR": "unknown", "VBELN": "SO-1"}) == "in_scope"
    assert refs.company_scope({"AUFNR": "3", "VBELN": "missing"}) == "pending"
    assert refs.company_scope({}) == "pending"
    assert refs.unique("production", sales="SO-1") is None
    assert refs.unique_company("sales", production="1") == "SO-1"
    assert SalesReferences([{"aufnr": "0003"}]).company_scope({"AUFNR": "3"}) == "in_scope"


def order(source="SG", **fields):
    rows = [
        {"MANDT": "800", "AUFNR": "000100", "VBELN": "SO-A", "PRODH": "00100"},
        {"MANDT": "800", "AUFNR": "000100", "VBELN": "SO-B", "PRODH": "00100"},
    ]
    return {"source": source, "records": rows, "data": rows[-1], **fields}


def test_all_details_remain_candidates_and_representative_is_not_duplicated():
    document = order()
    assert len(list(sales_lines(document))) == 2
    refs = SalesReferences([document])
    assert refs.resolve(production="100", sales="SO-A") == "in_scope"
    assert refs.unique("sales", production="100") is None


def test_filtered_sales_without_product_field_and_explicit_other_product():
    refs = SalesReferences([
        {"source": "SG", "data": {"AUFNR": "1", "VBELN": "A"}},
        {"source": "SG", "data": {"AUFNR": "2", "VBELN": "B", "PRODH": "00200"}},
    ])
    assert refs.resolve(production="1") == "in_scope"
    assert refs.resolve(production="2") == "out_of_scope"


def test_cross_source_or_zero_padding_collision_does_not_autofill():
    refs = SalesReferences([
        order(),
        order(source="KK", records=[{
            "MANDT": "600", "AUFNR": "100", "VBELN": "SO-C", "PRODH": "00100",
        }], data={}),
    ])
    assert refs.resolve(production="100") == "pending"
    assert refs.resolve(production="100", client="800") == "in_scope"
    assert refs.unique("sales", production="100") is None


def test_mixed_order_cannot_prove_scope_without_the_sales_relation():
    doc = order()
    doc["records"].append({"MANDT": "800", "AUFNR": "000100", "VBELN": "SO-C", "PRODH": "00200"})
    refs = SalesReferences([doc])
    assert refs.resolve(production="100") == "pending"
    assert refs.resolve(production="100", sales="SO-A") == "in_scope"
    assert refs.resolve(production="100", sales="SO-C") == "out_of_scope"


def test_conflicting_representative_cannot_hide_detail_conflict():
    doc = order(data={"MANDT": "800", "AUFNR": "000100", "VBELN": "SO-A", "PRODH": "00200"})
    assert SalesReferences([doc]).resolve(production="100", sales="SO-A") == "pending"
def test_loaded_legacy_reference_keeps_document_planned_date():
    from unittest.mock import Mock
    from scripts.sales_order_reference import SalesReferences

    document = {"source": "SG", "gstrs_date": "2026-10-01",
                "data": {"AUFNR": "1", "VBELN": "S1"}}
    collection = Mock()
    collection.find.side_effect = lambda query, projection: [
        {key: value for key, value in document.items() if projection.get(key)}
    ]
    refs = SalesReferences.load(collection)
    assert refs.unique("planned", production="1", sales="S1") == "2026-10-01"


