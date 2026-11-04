import pytest

from scripts.sync.syncctl import SyncError, resolve_tasks


def test_serial_is_excluded_from_default_and_explicit_mixed_runs():
    for requested in (None, ['serial_bindings', 'scs_change']):
        ids = [task.id for task in resolve_tasks(requested)]
        assert 'serial_bindings' not in ids
        assert 'sales_orders' in ids
        assert 'scs_doa' in ids and 'scs_change' in ids


def test_serial_only_request_reports_paused():
    with pytest.raises(SyncError, match='序列号绑定.*暂停'):
        resolve_tasks(['serial_bindings'])
