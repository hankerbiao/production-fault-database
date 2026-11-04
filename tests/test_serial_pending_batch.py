from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from scripts.sources.hana import hana_view_sync as hana
from test_sync_sales_orders import FakeDB, FakeMongoClient


@pytest.fixture
def serial_source(monkeypatch):
    db = FakeDB()
    db['orders'].docs = [{'source': 'SG', 'data': {'AUFNR': 'KNOWN'}}]
    client = FakeMongoClient()
    client.db = db
    monkeypatch.setenv('MONGODB_DATABASE', 'test')
    monkeypatch.setenv('TARGET_COLLECTION', 'orders')
    monkeypatch.setattr(hana, 'mongo_uri', lambda: 'mongodb://test')
    monkeypatch.setattr(hana, 'MongoClient', lambda *a, **k: client)
    rows = [(f'HOST-{i}', f'PART-{i}', f'UNKNOWN-{i}', 'x', '00100') for i in range(5)]
    rows.append(('HOST-K', 'PART-K', 'KNOWN', 'x', '00100'))

    @contextmanager
    def connection():
        remaining = iter([rows, []])
        yield SimpleNamespace(cursor=lambda: SimpleNamespace(
            execute=lambda *a: None, fetchmany=lambda size: next(remaining), close=lambda: None))

    monkeypatch.setattr(hana, 'hana_connection', connection)
    return db, hana.VIEW_SPECS['ZSGV_ZPP_SERNOLIST']


def test_serial_pending_is_batched_and_rechecked(serial_source, monkeypatch):
    db, spec = serial_source
    pending = db['sync_scope_pending']
    calls = []
    original_bulk_write = pending.bulk_write

    def counted_bulk_write(operations, **kwargs):
        calls.append(len(operations))
        return original_bulk_write(operations, **kwargs)

    monkeypatch.setattr(pending, 'bulk_write', counted_bulk_write)
    args = dict(mode='incremental', start_date=None, batch_size=2, lookback_days=2, dry_run=False)
    result = hana.sync_view(spec, **args)
    assert result['scope_pending'] == 5
    assert calls == [2, 2, 1]
    assert {d['payload']['AUFNR_HEAD'] for d in pending.docs} == {f'UNKNOWN-{i}' for i in range(5)}
    db['orders'].docs.append({'source': 'SG', 'data': {'AUFNR': 'UNKNOWN-0'}})
    result = hana.sync_view(spec, **args)
    assert result['scope_pending'] == 4
    assert {d['payload']['AUFNR_HEAD'] for d in pending.docs} == {f'UNKNOWN-{i}' for i in range(1, 5)}
    assert {d['AUFNR_HEAD'] for d in db[spec.collection].docs} == {'KNOWN', 'UNKNOWN-0'}


def test_serial_failed_pending_batch_does_not_commit_watermark(serial_source, monkeypatch):
    db, spec = serial_source
    def fail(*args, **kwargs):
        raise RuntimeError('pending write failed')
    monkeypatch.setattr(db['sync_scope_pending'], 'bulk_write', fail)
    with pytest.raises(RuntimeError, match='pending write failed'):
        hana.sync_view(spec, mode='incremental', start_date=None, batch_size=2, lookback_days=2, dry_run=False)
    assert db['sync_checkpoints'].docs == []
    assert db[spec.collection].docs == []
