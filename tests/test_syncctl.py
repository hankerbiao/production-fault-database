from __future__ import annotations

import pytest

from scripts.sync.syncctl import SyncError, is_retryable, json_result, resolve_tasks, validate_request


def test_resolve_tasks_adds_dependencies_in_stable_order():
    tasks = resolve_tasks(["scs_change", "repair_records"])
    assert [task.id for task in tasks] == ["sales_orders", "station_records", "repair_records", "scs_doa", "scs_change"]


def test_full_requires_start_date_and_rejects_unknown_tasks():
    with pytest.raises(SyncError, match="startDate"):
        validate_request("full", ["sales_orders"], None, None)
    with pytest.raises(SyncError, match="未知"):
        resolve_tasks(["not-a-task"])


def test_script_result_uses_last_json_line_and_preserves_contract():
    task = resolve_tasks(["sales_orders"])[0]
    result = json_result("diagnostic line\n{\"success\": true, \"run_id\": \"child\"}\n", task, "incremental")
    assert result == {"success": True, "run_id": "child", "taskId": "sales_orders", "mode": "incremental"}
    assert is_retryable({"error": "connection timed out"}, 1)
    assert not is_retryable({"error": {"retryable": False}}, 1)
