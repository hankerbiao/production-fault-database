from scripts.scs_doa_gstrs import derive_gstrs
from scripts.sales_order_reference import SalesReferences


def ref(*rows):
    return SalesReferences([{'source': 'KK', 'aufnr': n, 'data': {'AUFNR': n, 'GSTRS': d}} for n, d in rows])


def document(*orders):
    return {'details': {'equipment': [{'product_fields': {'生产订单': order}} for order in orders]}}


def test_single_production_order_matches_without_leading_zeros():
    result = derive_gstrs(document('000010349985'), ref(('10349985', '2026-09-18')))
    assert result['GSTRS'] == '2026-09-18'
    assert result['gstrs_status'] == 'matched'
    assert result['production_orders'] == ['10349985']


def test_unmatched_and_ambiguous_orders_never_receive_date():
    refs = ref(('10349985', '2026-09-18'), ('10349986', '2026-09-19'))
    assert derive_gstrs(document('000010349985', '000010349986'), refs)['gstrs_status'] == 'ambiguous'
    result = derive_gstrs(document('000010349985', '000010349987'), refs)
    assert result['gstrs_status'] == 'unmatched'
    assert result['GSTRS'] == ''
    assert derive_gstrs(document(''), refs)['gstrs_status'] == 'unmatched'


def test_collision_between_sources_is_not_silently_assigned():
    refs = SalesReferences([
        {'source': 'KK', 'aufnr': '10349985', 'data': {'AUFNR': '10349985', 'GSTRS': '2026-09-18'}},
        {'source': 'SG', 'aufnr': '10349985', 'data': {'AUFNR': '10349985', 'GSTRS': '2026-09-18'}},
    ])
    assert derive_gstrs(document('000010349985'), refs)['gstrs_status'] == 'ambiguous'


def test_backfill_dry_run_does_not_write_and_apply_clears_stale_dates():
    from scripts.scs_doa_gstrs import backfill
    class Collection:
        def __init__(self):
            self.rows = [{'_id': 'a', **document('000010349985')}, {'_id': 'b', 'GSTRS': '2020-01-01', **document('000099999999')}]
            self.writes = []
        def find(self, *args):
            return iter(self.rows)
        def bulk_write(self, operations, ordered=False):
            self.writes.extend(operations)
            return type('Result', (), {'matched_count': len(operations), 'upserted_count': 0, 'modified_count': len(operations)})()
    collection = Collection()
    refs = ref(('10349985', '2026-09-18'))
    assert backfill(collection, refs, apply=False)['matched'] == 1
    assert collection.writes == []
    result = backfill(collection, refs, apply=True)
    assert result['unmatched'] == 1
    assert collection.writes[1]._doc['$set']['GSTRS'] == ''


def test_sync_orchestrator_runs_backfill_after_scs_and_sales():
    from scripts.sync.syncctl import resolve_tasks
    tasks = resolve_tasks(['scs_doa_gstrs'])
    ids = [task.id for task in tasks]
    assert ids.index('sales_orders') < ids.index('scs_doa') < ids.index('scs_doa_gstrs')
    command = tasks[-1].command('python', 'incremental', None, None)
    assert '--apply' in command


def test_single_scs_sync_also_schedules_planned_date_backfill():
    from scripts.sync.syncctl import resolve_tasks
    assert [task.id for task in resolve_tasks(['scs_doa'])][-1] == 'scs_doa_gstrs'
