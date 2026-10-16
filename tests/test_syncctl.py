from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest

from scripts.sync import syncctl
from scripts.sync.syncctl import SyncError, is_retryable, json_result, resolve_tasks, validate_request


def test_skip_scs_keeps_all_other_tasks_in_stable_order():
    tasks = resolve_tasks(None, skip_scs=True)
    assert [task.id for task in tasks] == [
        "sales_orders", "station_records", "repair_records",
        "order_bom_postings", "order_bom_planned_start", "serial_bindings",
    ]
    assert {"scs_doa", "scs_change"} <= {task.id for task in resolve_tasks(None)}


def test_skip_scs_with_explicit_tasks_preserves_other_dependencies():
    tasks = resolve_tasks(["scs_change", "repair_records"], skip_scs=True)
    assert [task.id for task in tasks] == [
        "sales_orders", "station_records", "repair_records",
    ]


@pytest.mark.parametrize("requested", [["scs_doa"], ["scs_change"], ["scs_doa", "scs_change"]])
def test_skip_scs_rejects_empty_task_plan(requested):
    with pytest.raises(SyncError, match="没有可执行") as raised:
        resolve_tasks(requested, skip_scs=True)
    assert raised.value.code == "invalid_request"


def test_skip_scs_still_rejects_unknown_tasks():
    with pytest.raises(SyncError, match="未知同步任务"):
        resolve_tasks(["not-a-task"], skip_scs=True)


@pytest.mark.parametrize("mode,start", [("incremental", None), ("full", "2026-01-01")])
def test_skip_scs_is_applied_during_request_validation(mode, start):
    tasks, actual_start, end = validate_request(
        mode, ["scs_change", "repair_records"], start, "2026-10-15", skip_scs=True,
    )
    assert [task.id for task in tasks] == [
        "sales_orders", "station_records", "repair_records",
    ]
    assert actual_start == start
    assert end == "2026-10-15"


def test_orchestrator_skip_scs_executes_and_records_only_remaining_tasks(monkeypatch):
    store = Mock()
    store.run.return_value = {"state": "success"}
    lease = Mock(lost=False)
    monkeypatch.setattr(syncctl, "RunStore", lambda _db: store)
    monkeypatch.setattr(syncctl, "Lease", lambda *_args: lease)
    runner = syncctl.Orchestrator(object())
    execute_task = Mock(return_value={"success": True})
    monkeypatch.setattr(runner, "_execute_task", execute_task)

    result = runner.execute(mode="incremental", source="cron", skip_scs=True)

    expected = [
        "sales_orders", "station_records", "repair_records",
        "order_bom_postings", "order_bom_planned_start", "serial_bindings",
    ]
    assert result["state"] == "success"
    assert [call.args[1].id for call in execute_task.call_args_list] == expected
    assert store.create.call_args.kwargs["requested"] == expected
    assert [task.id for task in store.create.call_args.kwargs["tasks"]] == expected
    lease.close.assert_called_once()


def test_cli_forwards_skip_scs_to_orchestrator(monkeypatch, capsys):
    client = MagicMock()
    runner = Mock()
    runner.execute.return_value = {"state": "success"}
    monkeypatch.setattr(syncctl.sys, "argv", [
        "syncctl.py", "run", "--mode", "incremental", "--source", "cron", "--skip-scs",
    ])
    monkeypatch.setattr(syncctl, "load_dotenv", Mock())
    monkeypatch.setattr(syncctl, "install_graceful_shutdown", Mock())
    monkeypatch.setattr(syncctl, "mongo_uri", lambda: "mongodb://unused")
    monkeypatch.setattr(syncctl, "mongo_client_options", lambda: {})
    monkeypatch.setattr(syncctl, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(syncctl, "Orchestrator", lambda _db: runner)
    monkeypatch.setenv("MONGODB_DATABASE", "test_syncctl")

    assert syncctl.main() == 0
    assert runner.execute.call_args.kwargs["skip_scs"] is True
    assert runner.execute.call_args.kwargs["source"] == "cron"
    client.close.assert_called_once()
    assert '"state": "success"' in capsys.readouterr().out


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
