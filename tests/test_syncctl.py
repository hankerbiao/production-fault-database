from __future__ import annotations

import pytest

from scripts.sync.syncctl import SyncError, is_retryable, json_result, resolve_tasks, validate_request


def test_resolve_tasks_adds_dependencies_in_stable_order():
    tasks = resolve_tasks(["scs_change", "repair_records"])
    assert [task.id for task in tasks] == [
        "sales_orders", "station_records", "repair_records",
        "scs_doa", "scs_change",
    ]


@pytest.mark.parametrize("requested", ["scs_doa", "scs_change"])
def test_scs_classification_does_not_depend_on_delivery_or_sales_orders(requested):
    tasks = [task.id for task in resolve_tasks([requested])]
    assert tasks[0] == "scs_doa"
    assert "sales_order_delivery_records" not in tasks
    assert "sales_orders" not in tasks
    assert tasks[-1] == requested


def test_full_requires_start_date_and_rejects_unknown_tasks():
    with pytest.raises(SyncError, match="startDate"):
        validate_request("full", ["sales_orders"], None, None)
    with pytest.raises(SyncError, match="未知"):
        resolve_tasks(["not-a-task"])


def test_planned_start_task_resolves_bom_dependency_and_reports_run_id():
    tasks = resolve_tasks(["order_bom_planned_start"])
    assert [task.id for task in tasks] == ["sales_orders", "order_bom_postings", "order_bom_planned_start"]
    command = tasks[-1].command("python", "incremental", None, None, "run-1")
    assert command[-3:] == ["--apply", "--run-id", "run-1"]


def test_retired_delivery_task_is_excluded_from_all_runs_and_explicit_requests():
    assert "sales_order_delivery_records" not in [task.id for task in resolve_tasks(None)]
    with pytest.raises(SyncError, match="未知同步任务"):
        resolve_tasks(["sales_order_delivery_records"])


def test_script_result_uses_last_json_line_and_preserves_contract():
    task = resolve_tasks(["sales_orders"])[0]
    result = json_result("diagnostic line\n{\"success\": true, \"run_id\": \"child\"}\n", task, "incremental")
    assert result == {"success": True, "run_id": "child", "taskId": "sales_orders", "mode": "incremental"}
    assert is_retryable({"error": "connection timed out"}, 1)
    assert not is_retryable({"error": {"retryable": False}}, 1)
