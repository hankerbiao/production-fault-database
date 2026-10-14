from scripts.maintenance.refresh_scs_5000_company import build_refresh_plan


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, *_args, **_kwargs):
        return list(self.documents)


class Database:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


def test_refresh_plan_counts_doa_and_dependent_change_updates():
    database = Database({
        "doa": Collection([
            {
                "_id": "doa-5000",
                "sales_order": "OLD",
                "details": {
                    "fields": {"合同/订单号": "PO-5000"},
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
            {"so_code": "SH-5000", "is_5000_company": False},
            {"so_code": "SH-OTHER", "is_5000_company": True},
        ]),
    })

    plan = build_refresh_plan(
        database,
        doa_collection="doa",
        change_collection="changes",
        customer_purchase_orders={"PO-5000"},
    )

    assert plan == {"doa_would_update": 2, "change_would_update": 2}
