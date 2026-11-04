#!/usr/bin/env python3
"""Back up and clean the complete repair collection using the shared merge rules."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import BSON
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sources.hana.hana_view_sync import env, load_dotenv, mongo_client_options, mongo_uri, process_lock
from scripts.sync.sync_sales_orders import cleanup_repair_duplicates
from scripts.sync_runtime import check_leases, resource_lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_dotenv()
    client = MongoClient(mongo_uri(), **mongo_client_options())
    try:
        db = client[env('MONGODB_DATABASE')]
        name = env('REPAIR_COLLECTION', 'repair_records_sap')
        collection = db[name]
        if not args.apply:
            print(json.dumps({'mode': 'preview', 'documents': collection.count_documents({}),
                              **cleanup_repair_duplicates(collection, {}, apply=False)}, ensure_ascii=False))
            return 0
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = ROOT / 'outputs' / f'repair_cleanup_backup_{stamp}.bson'
        backup.parent.mkdir(parents=True, exist_ok=True)
        with process_lock(env('REPAIR_STATION_BACKFILL_LOCK_PATH', '/tmp/line-fault-station-order-backfill.lock')):
            with process_lock(env('SYNC_LOCK_PATH', '/tmp/line-fault-table-sync.lock')):
                with resource_lock(db, name):
                    count, digest = 0, hashlib.sha256()
                    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as output:
                        for document in collection.find({}):
                            check_leases()
                            raw = BSON.encode(document)
                            output.write(raw)
                            digest.update(raw)
                            count += 1
                        output.flush()
                        os.fsync(output.fileno())
                    if count != collection.count_documents({}):
                        raise RuntimeError('备份数量与集合数量不一致，拒绝清理')
                    verify = hashlib.sha256()
                    with backup.open('rb') as saved:
                        for block in iter(lambda: saved.read(1024 * 1024), b''):
                            verify.update(block)
                    if verify.digest() != digest.digest():
                        raise RuntimeError('备份校验失败，拒绝清理')
                    print(json.dumps({'stage': 'backup_ready', 'backup': str(backup),
                                      'documents': count, 'sha256': digest.hexdigest()}, ensure_ascii=False), flush=True)
                    result = cleanup_repair_duplicates(collection, {}, strict_guard=True)
                    print(json.dumps({'mode': 'apply', 'backup': str(backup), 'backup_documents': count,
                                      'backup_sha256': digest.hexdigest(), **result,
                                      'remaining_documents': collection.count_documents({})}, ensure_ascii=False))
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
