"""Resolve SCS equipment production orders to unambiguous planned start dates."""
from datetime import date
from typing import Any

from scripts.sales_order_reference import SalesReferences
from scripts.sync_runtime import order_key


def derive_gstrs(document: dict[str, Any], refs: SalesReferences) -> dict[str, Any]:
    details = document.get('details') or {}
    equipment = details.get('equipment') or [] if isinstance(details, dict) else []
    values = [(row.get('product_fields') or {}).get('生产订单') for row in equipment if isinstance(row, dict)]
    raw = [str(value or '').strip() for value in values]
    numbers = sorted({order_key(value) for value in raw if value.isdigit() and int(value) != 0})
    result = {'GSTRS': '', 'gstrs_status': 'unmatched', 'production_orders': numbers}
    if not raw or any(not value.isdigit() or int(value) == 0 for value in raw):
        return result
    dates = set()
    for number in numbers:
        candidates = refs.candidates(production=number)
        if not candidates:
            return result
        identities = {(row['source'], row['client'], order_key(row['production'])) for row in candidates}
        if len(identities) != 1 or any(row['scope'] != 'in_scope' for row in candidates):
            return {**result, 'gstrs_status': 'ambiguous'}
        for row in candidates:
            try:
                dates.add(date.fromisoformat(str(row['planned'])[:10].replace('/', '-')).isoformat())
            except ValueError:
                return result
    if len(dates) != 1:
        return {**result, 'gstrs_status': 'ambiguous'}
    return {**result, 'GSTRS': dates.pop(), 'gstrs_status': 'matched'}


def backfill(collection: Any, refs: SalesReferences, *, apply: bool = False) -> dict[str, int]:
    from pymongo import UpdateOne
    from scripts.sync_runtime import verified_bulk_write

    counts = {'matched': 0, 'unmatched': 0, 'ambiguous': 0, 'updated': 0}
    operations = []
    cursor = collection.find({}, {'details': 1, 'GSTRS': 1, 'gstrs_status': 1, 'production_orders': 1})
    try:
        for doc in cursor:
            derived = derive_gstrs(doc, refs)
            counts[derived['gstrs_status']] += 1
            if any(doc.get(key) != value for key, value in derived.items()):
                counts['updated'] += 1
                if apply:
                    operations.append(UpdateOne({'_id': doc['_id']}, {'$set': derived}))
                    if len(operations) >= 500:
                        verified_bulk_write(collection, operations)
                        operations.clear()
        if operations:
            verified_bulk_write(collection, operations)
    finally:
        if hasattr(cursor, 'close'):
            cursor.close()
    return counts
