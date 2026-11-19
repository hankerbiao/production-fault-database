import json
from unittest.mock import patch

from scripts.sync import syncctl


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return b'{}'


def test_failure_notification_contains_failed_task_and_reason(monkeypatch):
    monkeypatch.setenv("GQUAN_APP_TOKEN", "test-token")
    monkeypatch.setenv("GQUAN_APP_TO", "libiao1")
    with patch.object(syncctl, "urlopen", return_value=Response()) as send:
        result = syncctl.notify_failed_run({"_id": "run-1", "stages": [
            {"state": "failed", "label": "工位过站", "error": {"message": "连接中断"}}
        ]})
    request = send.call_args.args[0]
    payload = json.loads(request.data)
    assert result == {"success": True, "status": 200}
    assert payload["to"] == ["libiao1"]
    assert payload["msg_type"] == "AGENDA"
    assert all(value in payload["desc"] for value in ("run-1", "工位过站", "连接中断"))


def test_notification_failure_is_reported_without_raising(monkeypatch):
    monkeypatch.setenv("GQUAN_APP_TOKEN", "test-token")
    with patch.object(syncctl, "urlopen", side_effect=OSError("network")):
        result = syncctl.notify_sync_failure("run-1", "工位过站", "连接中断")
    assert result["success"] is False
    assert "OSError" in result["error"]


def test_orchestrator_failure_notifies_once_and_records_result(monkeypatch):
    writes = []

    class Collection:
        def update_one(self, selector, update):
            writes.append((selector, update))

    class Client:
        def __getitem__(self, key):
            return self if key != syncctl.RUN_COLLECTION else Collection()

        def close(self):
            pass

    class Orchestrator:
        def __init__(self, db):
            pass

        def execute(self, **kwargs):
            return {"_id": "run-1", "state": "failed", "stages": [
                {"state": "failed", "label": "工位过站", "error": {"message": "连接中断"}}
            ]}

    monkeypatch.setattr(syncctl.sys, "argv", ["syncctl.py", "run"])
    monkeypatch.setattr(syncctl, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(syncctl, "install_graceful_shutdown", lambda: None)
    monkeypatch.setattr(syncctl, "mongo_uri", lambda: "mongodb://unused")
    monkeypatch.setattr(syncctl, "mongo_client_options", lambda: {})
    monkeypatch.setattr(syncctl, "MongoClient", lambda *args, **kwargs: Client())
    monkeypatch.setattr(syncctl, "Orchestrator", Orchestrator)
    with patch.object(syncctl, "notify_sync_failure", return_value={"success": True}) as notify:
        assert syncctl.main() == 1
    notify.assert_called_once_with("run-1", "工位过站", "连接中断")
    assert writes == [({"_id": "run-1"}, {"$set": {"notification": {"success": True}}})]
