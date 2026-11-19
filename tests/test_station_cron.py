from types import SimpleNamespace

from scripts.sync import station_cron


def test_failed_station_sync_notifies_once(monkeypatch):
    sent = []
    monkeypatch.setattr(station_cron, "load_dotenv", lambda: None)
    monkeypatch.setattr(station_cron.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='{"success":false,"error":"connection reset"}', stderr=""))
    monkeypatch.setattr(station_cron, "notify_sync_failure", lambda *args: sent.append(args) or {"success": True})
    assert station_cron.main() == 1
    assert len(sent) == 1
    assert sent[0][2] == "connection reset"


def test_successful_station_sync_does_not_notify(monkeypatch):
    monkeypatch.setattr(station_cron, "load_dotenv", lambda: None)
    monkeypatch.setattr(station_cron.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='{"success":true}', stderr=""))
    monkeypatch.setattr(station_cron, "notify_sync_failure", lambda *args: (_ for _ in ()).throw(AssertionError()))
    assert station_cron.main() == 0
