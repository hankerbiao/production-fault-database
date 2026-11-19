from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.sync import station_records_sales_orders as sync


class Cursor:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.current = []
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        self.current = list(next(self.batches))

    def fetchmany(self, size):
        result, self.current = self.current[:size], self.current[size:]
        return result

    def close(self):
        pass


def setup(monkeypatch, batches, checkpoint=None):
    cursor = Cursor([[tuple(checkpoint['cursor']) if checkpoint else ('1', 'OP', 't', '', '10')]] + batches)
    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(sync.hana, 'hana_connection', connection)
    monkeypatch.setattr(sync, 'check_leases', lambda **kwargs: None)
    state = Mock()
    state.find_one.return_value = checkpoint
    target = Mock()
    target.bulk_write.return_value = SimpleNamespace(upserted_count=1, matched_count=0, modified_count=0)
    db = {'sync_checkpoints': state, 'target': target, 'sales_orders_sap': Mock()}
    spec = SimpleNamespace(collection='target', qualified_name='station', columns=sync.FIELDS, view_id='station')
    return db, spec, cursor, state, target


def test_key_empty_pcode_is_stable_and_other_fields_required():
    row = dict(zip(sync.FIELDS, ('1', 'OP', '2026-01-01', '', '20')))
    assert sync.key_for(row) == sync.key_for({**row, 'OTHER': 'changed'})
    with pytest.raises(ValueError):
        sync.key_for({**row, 'AUFNR': ''})


def test_boundary_has_strict_lower_inclusive_upper():
    where, params = sync.range_clause(('1', 'a', 't', '', '2'), ('2', 'b', 't', '', '3'))
    assert 'TRIM(COALESCE("AUFNR", \'\')) >= ?' not in where
    assert 'TRIM(COALESCE("AUFNR", \'\')) <= ?' in where
    assert len(params) == 30


def test_initialization_uses_latest_local_record(monkeypatch):
    start, end = ('1', 'OP', 't', '', '10'), ('2', 'OP', 't', '', '20')
    db, spec, cursor, state, target = setup(monkeypatch, [[end], [('20',)]])
    target.find_one.return_value = dict(zip(sync.FIELDS, start))
    result = sync.sync_incremental(db, spec, batch_size=10, dry_run=False, load_orders=lambda c: [])
    assert result['initialized'] and result['range']['start'] == start
    assert target.find_one.call_args.kwargs['sort'] == [(field, -1) for field in sync.FIELDS]
    assert len(cursor.queries) == 3
    assert state.update_one.call_args.args[1]['$set']['cursor'] == list(start)


def test_detail_queries_split_orders_and_commit_after_all_batches(monkeypatch):
    monkeypatch.setenv('STATION_ORDER_BATCH_SIZE', '1')
    start = ('1', 'OP', 't', '', '10')
    first = ('2', 'OP', 't', '', '20')
    second = ('3', 'OP', 't', '', '30')
    db, spec, cursor, state, target = setup(
        monkeypatch, [[second], [('20',), ('30',)], [first], [second]], {'cursor': start}
    )
    result = sync.sync_incremental(db, spec, batch_size=10, dry_run=False, load_orders=lambda c: ['20', '30'])
    assert result['source_rows'] == 2
    assert len(cursor.queries) == 5
    assert target.bulk_write.call_count == 2
    assert state.update_one.call_args.args[1]['$set']['cursor'] == list(second)


def test_later_detail_batch_failure_does_not_advance_checkpoint(monkeypatch):
    monkeypatch.setenv('STATION_ORDER_BATCH_SIZE', '1')
    start = ('1', 'OP', 't', '', '10')
    first = ('2', 'OP', 't', '', '20')
    end = ('3', 'OP', 't', '', '30')
    db, spec, cursor, state, target = setup(
        monkeypatch, [[end], [('20',), ('30',)], [first]], {'cursor': start}
    )
    execute = cursor.execute
    def fail_second_detail(sql, params=()):
        if len(cursor.queries) == 4:
            raise RuntimeError('HANA timeout')
        execute(sql, params)
    cursor.execute = fail_second_detail
    with pytest.raises(RuntimeError, match='HANA timeout'):
        sync.sync_incremental(db, spec, batch_size=10, dry_run=False, load_orders=lambda c: ['20', '30'])
    assert target.bulk_write.call_count == 1
    state.update_one.assert_not_called()


def test_unmatched_orders_advance_cursor_without_detail_query(monkeypatch):
    start, end = ('1', 'OP', 't', '', '10'), ('2', 'OP', 't', '', '20')
    db, spec, cursor, state, _ = setup(monkeypatch, [[end], [('20',)]], {'cursor': start})
    result = sync.sync_incremental(db, spec, batch_size=10, dry_run=False, load_orders=lambda c: [])
    assert result['candidate_orders'] == 1 and result['matched_orders'] == 0
    assert len(cursor.queries) == 3
    assert state.update_one.call_args.args[1]['$set']['cursor'] == list(start)


def test_partial_write_failure_does_not_advance_checkpoint(monkeypatch):
    start, end = ('1', 'OP', 't', '', '10'), ('2', 'OP', 't', '', '20')
    db, spec, _, state, target = setup(monkeypatch, [[end], [('20',)], [end]], {'cursor': start})
    target.bulk_write.side_effect = RuntimeError('write failed')
    with pytest.raises(RuntimeError, match='write failed'):
        sync.sync_incremental(db, spec, batch_size=1, dry_run=False, load_orders=lambda c: ['20'])
    state.update_one.assert_not_called()


def test_duplicate_source_keys_stop_before_checkpoint(monkeypatch):
    start, end = ('1', 'OP', 't', '', '10'), ('2', 'OP', 't', '', '20')
    db, spec, _, state, _ = setup(monkeypatch, [[end], [('20',)], [end, end]], {'cursor': start})
    with pytest.raises(RuntimeError, match='业务键重复'):
        sync.sync_incremental(db, spec, batch_size=1, dry_run=False, load_orders=lambda c: ['20'])
    state.update_one.assert_not_called()


def test_empty_source_retains_local_start_and_preview_does_not_write(monkeypatch):
    start = ('1', 'OP', 't', '', '10')
    db, spec, _, state, target = setup(monkeypatch, [[]])
    target.find_one.return_value = dict(zip(sync.FIELDS, start))
    sync.sync_incremental(db, spec, batch_size=10, dry_run=False, load_orders=lambda c: [])
    assert state.update_one.call_args.args[1]['$set']['cursor'] == list(start)
    db, spec, _, state, target = setup(monkeypatch, [[]])
    target.find_one.return_value = dict(zip(sync.FIELDS, start))
    sync.sync_incremental(db, spec, batch_size=10, dry_run=True, load_orders=lambda c: [])
    state.update_one.assert_not_called()


def test_multiple_batches_and_checkpoint_failure(monkeypatch):
    start = ('1', 'OP', 't', '', '10')
    row = ('2', 'OP', 't', '', '20')
    end = ('3', 'OP', 't', '', '20')
    db, spec, _, state, target = setup(monkeypatch, [[end], [('20',)], [row, end]], {'cursor': start})
    state.update_one.side_effect = RuntimeError('checkpoint failed')
    with pytest.raises(RuntimeError, match='checkpoint failed'):
        sync.sync_incremental(db, spec, batch_size=1, dry_run=False, load_orders=lambda c: ['20'])
    assert target.bulk_write.call_count == 2



def test_missing_source_cursor_fails_without_advancing(monkeypatch):
    start = ('1', 'OP', 't', '', '10')
    db, spec, cursor, state, _ = setup(monkeypatch, [], {'cursor': start})
    cursor.batches = iter([[]])
    with pytest.raises(RuntimeError, match='游标起点不存在'):
        sync.sync_incremental(db, spec, batch_size=10, dry_run=False, load_orders=lambda c: [])
    state.update_one.assert_not_called()
