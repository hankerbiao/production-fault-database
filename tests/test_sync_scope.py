from datetime import date
from types import SimpleNamespace

import pytest

from scripts.sync import sync_sales_orders as sync
from scripts.sync_runtime import checkpoint_id, sales_scope_checkpoint_id


def test_only_sales_checkpoints_bind_to_00100_scope():
    assert checkpoint_id("scs_doa", "scs") == "scs_doa:scs"
    assert checkpoint_id("hana", "ZSGV_ZPP_SERNOLIST") == "hana:ZSGV_ZPP_SERNOLIST"
    assert sales_scope_checkpoint_id("sales_orders", "SG") == "sales_orders:SG:prodh-00100:v1"


@pytest.mark.parametrize("values", [None, [], ["00200"], ["00100", "00200"]])
def test_payload_cannot_omit_or_expand_scope(values):
    with pytest.raises(ValueError, match="00100"):
        sync.payload(date(2026, 1, 1), date(2026, 1, 2), values)


def test_all_prodh_is_rejected_before_opening_clients(monkeypatch):
    monkeypatch.setattr(sync, "MongoClient", lambda *a, **k: pytest.fail("unexpected database access"))
    args = sync.build_parser().parse_args(["--full", "--start-date", "2026-01-01", "--all-prodh"])
    with pytest.raises(ValueError, match="00100"):
        sync.sync_sales(args)


def test_environment_cannot_expand_scope_even_with_correct_cli(monkeypatch):
    monkeypatch.setenv("SOURCE_PRODH_LIST", "00200")
    with pytest.raises(ValueError, match="00100"):
        sync.configured_prodh(SimpleNamespace(all_prodh=False, prodh_list=["00100"]))
