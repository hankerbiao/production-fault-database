from contextlib import nullcontext
from types import SimpleNamespace

from scripts.maintenance.refresh_scs_5000_company import build_refresh_plan, refresh


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, *_args, **_kwargs):
        query = _args[0] if _args else {}
        return [dict(document) for document in self.documents
                if all(document.get(key) == value for key, value in query.items())]

    def bulk_write(self, operations, ordered=False):
        modified = 0
        for operation in operations:
            document = next(row for row in self.documents if row["_id"] == operation._filter["_id"])
            values = operation._doc["$set"]
            modified += any(document.get(key) != value for key, value in values.items())
            document.update(values)
        return SimpleNamespace(modified_count=modified)


class Database:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


def make_database():
    return Database({
        "doa": Collection([
            {
                "_id": "doa-5000",
                "sales_order": "OLD",
                "details": {
                    "fields": {"合同/订单号": "XNC251957"},
                    "service_fields": {"服务单号": "SH-5000"},
                },
                "service_uid": "SH-5000",
                "is_5000_company": False,
            },
            {
                "_id": "doa-other",
                "details": {"fields": {"合同/订单号": "PO-OTHER"}},
                "is_5000_company": True,
            },
        ]),
        "changes": Collection([
            {"_id": "c1", "so_code": "SH-5000", "is_5000_company": False},
            {"_id": "c2", "so_code": "SH-OTHER", "is_5000_company": True},
        ]),
    })


def test_refresh_plan_counts_doa_and_dependent_change_updates():
    plan = build_refresh_plan(
        make_database(),
        doa_collection="doa",
        change_collection="changes",
    )

    assert plan == {"doa_would_update": 2, "change_would_update": 2}


def test_refresh_without_delivery_collection_previews_applies_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("SCS_DOA_COLLECTION", "doa")
    monkeypatch.setenv("SCS_CHANGE_COLLECTION", "changes")
    monkeypatch.setattr("scripts.maintenance.refresh_scs_5000_company.resource_lock",
                        lambda *_args: nullcontext())
    database = make_database()
    preview = refresh(database, apply=False)
    assert preview["doa_would_update"] == preview["change_would_update"] == 2
    assert database["doa"].documents[0]["is_5000_company"] is False
    assert database["changes"].documents[0]["is_5000_company"] is False

    result = refresh(database, apply=True)
    assert result["doa_updated"] == result["change_updated"] == 2
    assert result["company_service_orders"] == 1
    assert database["doa"].documents[0]["sales_order"] == "XNC251957"
    assert database["doa"].documents[0]["is_5000_company"] is True
    assert database["doa"].documents[1]["is_5000_company"] is False
    assert database["changes"].documents[0]["is_5000_company"] is True
    assert database["changes"].documents[1]["is_5000_company"] is False

    repeated = refresh(database, apply=True)
    assert repeated["doa_would_update"] == repeated["change_would_update"] == 0
    assert repeated["doa_updated"] == repeated["change_updated"] == 0
