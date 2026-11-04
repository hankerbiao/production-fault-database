from datetime import datetime, timedelta, timezone

import pytest

from scripts.sources import scs_doa_sync as doa
from scripts.sources import scs_change_sync as change


def test_equipment_context_is_decoded_and_bound_to_its_own_row():
    rows = doa.extract_equipment("""
      <a href="?contextKey=so_sub_service_order&contextValue=WRONG">unrelated</a>
      <form id="a8a80e692922312b1019226d5b47200b5"><table>
        <tr><th name="mtr_sn">SN</th></tr>
        <tr><td><a href="?contextKey=so_sub_service_order&amp;contextValue=ctx-2">SN-B</a></td></tr>
        <tr value="ctx-1"><td>SN-A</td></tr>
      </table></form>""")
    assert [(row["material_sn"], row["context_value"]) for row in rows] == [
        ("SN-B", "ctx-2"), ("SN-A", "ctx-1"),
    ]


def test_equipment_missing_required_context_is_not_silently_accepted():
    with pytest.raises(RuntimeError, match="上下文"):
        doa.extract_equipment("""
          <form id="a8a80e692922312b1019226d5b47200b5"><table>
          <tr><th name="mtr_sn">SN</th></tr><tr><td>SN-A</td></tr></table></form>""")


def test_detail_ttl_and_full_refresh(monkeypatch):
    now = datetime.now(timezone.utc)
    row = {"doa_code": "D1"}
    existing = {**row, "details": {"fields": {"订单号": "A"}},
                "_detail_schema_version": doa.DETAIL_SCHEMA_VERSION, "_detail_fetched_at": now}
    assert not doa.should_refresh_details(existing, row)
    assert doa.should_refresh_details(existing, row, full=True)
    existing["_detail_fetched_at"] = now - timedelta(days=8)
    assert doa.should_refresh_details(existing, row)


def test_skipped_detail_does_not_rewrite_success_or_attempt_time(monkeypatch):
    now = datetime.now(timezone.utc)
    row = {"doa_code": "D1"}
    existing = {**row, "details": {"fields": {"订单号": "A"}},
                "_detail_schema_version": doa.DETAIL_SCHEMA_VERSION, "_detail_fetched_at": now}
    result = doa.refreshed_details(None, row, existing, {"detail_errors": 0, "skipped_details": 0})
    assert "_detail_attempted_at" not in result
    assert "_detail_fetched_at" not in result


def test_repeated_change_page_aborts_instead_of_looping(monkeypatch):
    row = {"so_code": "S1"}
    monkeypatch.setattr(change, "fetch_change_page", lambda *a: [row])
    pages = change.iter_changes(None, page_size=1)
    assert next(pages) == [row]
    with pytest.raises(RuntimeError, match="重复"):
        next(pages)


def test_nonempty_but_incomplete_doa_page_preserves_valid_details(monkeypatch):
    from types import SimpleNamespace

    existing = {"details": {"fields": {"订单号": "XNC1", "问题描述": "valid"}}}
    response = SimpleNamespace(
        text="<table><tr><th>申报单号:</th><td>D1</td></tr></table>",
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(doa, "request", lambda *a, **k: response)
    result = doa.refreshed_details(None, {"doa_code": "D1", "_context_value": "ctx"}, existing,
                                   {"detail_errors": 0, "skipped_details": 0}, full=True)
    assert result["_detail_pending"]
    assert result["details"] == existing["details"]
    assert "_detail_fetched_at" not in result


def test_partial_refresh_cannot_silently_drop_existing_detail_fields(monkeypatch):
    existing = {"details": {"fields": {"订单号": "XNC1", "问题描述": "valid"}}}
    monkeypatch.setattr(doa, "fetch_detail", lambda *a: {"fields": {"订单号": "XNC1"}})
    result = doa.refreshed_details(None, {"doa_code": "D1"}, existing,
                                   {"detail_errors": 0, "skipped_details": 0}, full=True)
    assert result["_detail_pending"]
    assert result["details"] == existing["details"]
