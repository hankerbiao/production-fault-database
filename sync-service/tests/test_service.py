import os
import tempfile
from pathlib import Path

os.environ['SYNC_SERVICE_STATE_DIR'] = tempfile.mkdtemp(prefix='sync-service-test-')

from fastapi.testclient import TestClient
import app


def test_schedule_validation_and_persistence():
    with TestClient(app.app) as client:
        items = client.get('/api/schedule').json()['items']
        assert all(item['enabled'] for item in items)
        invalid = [dict(item) for item in items]
        invalid[0].update(enabled=True, times=[])
        assert client.put('/api/schedule', json={'items': invalid}).status_code == 400
        invalid[0]['times'] = ['25:00']
        assert client.put('/api/schedule', json={'items': invalid}).status_code == 400
        items[0].update(enabled=True, times=['08:00', '18:00'])
        items[1].update(enabled=True, times=['18:00'])
        assert client.put('/api/schedule', json={'items': items}).json()['items'] == items
    with TestClient(app.app) as client:
        assert client.get('/api/schedule').json()['items'] == items


def test_due_tasks_are_combined_and_queued_when_busy(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    class Collection:
        def find_one(self, query): return {'state': 'running'}
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(app, 'mongo', lambda: Client())
    monkeypatch.setattr(app, 'collection', lambda client: Collection())
    with TestClient(app.app) as client:
        items = client.get('/api/schedule').json()['items']
        for i, item in enumerate(items):
            item.update(enabled=i < 2, times=['18:00'] if i < 2 else [])
        client.put('/api/schedule', json={'items': items})
        sunday = datetime(2026, 11, 15, 18, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        app.schedule_tick(sunday)
        app.schedule_tick(sunday)
        with app.connect() as db:
            rows = db.execute('SELECT * FROM schedule_queue WHERE slot=?', ('2026-11-15 18:00',)).fetchall()
        assert len(rows) == 1
        assert set(app.json.loads(rows[0]['tasks'])) == {items[0]['taskId'], items[1]['taskId']}
        assert rows[0]['state'] == 'pending'
        monkeypatch.setattr(app, 'collection', lambda client: type('Available', (), {'find_one': lambda self, query: None})())
        started = []
        monkeypatch.setattr(app, 'start_run', lambda request, source: started.append((request.taskIds, source)))
        app.schedule_tick(sunday)
        app.schedule_tick(sunday)
        assert len(started) == 1



def test_invalid_requests_and_local_launch_record(monkeypatch):
    class Child:
        pid = 123
        def wait(self):
            return 2
    monkeypatch.setattr(app.subprocess, 'Popen', lambda *args, **kwargs: Child())
    with TestClient(app.app) as client:
        assert client.post('/api/runs', json={'mode': 'full', 'startDate': '2026-01-01'}).status_code == 400
        assert client.post('/api/runs', json={'taskIds': ['unknown']}).status_code == 400
        response = client.post('/api/runs', json={'taskIds': ['sales_orders']})
        assert response.status_code == 202
        run_id = response.json()['runId']
        assert any(item['id'] == run_id for item in client.get('/api/launches').json()['items'])
        assert client.get(f'/api/launches/{run_id}/log').status_code == 200
        assert client.get('/api/runs/x/logs/unknown').status_code == 400
