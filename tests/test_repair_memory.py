import tracemalloc
import resource
import sys

from test_repair_snapshot import repair, run, seed_db, source


def test_target_index_memory_does_not_grow_with_total_documents(monkeypatch):
    peaks = []
    rss = []
    disk = []
    for count in (10000, 100000):
        db = seed_db(monkeypatch)
        target = db["repairs"]

        def documents(query, *args):
            for index in range(count):
                yield {**repair(PCODE=f"PC-{index}"), "_id": str(index),
                       "_source_view": "ZSGV_ZZT_WLJL"}

        monkeypatch.setattr(target, "find", documents)
        source(monkeypatch, [])
        tracemalloc.start()
        try:
            result = run(db, dry_run=True)
            _, peak = tracemalloc.get_traced_memory()
            peaks.append(peak)
        finally:
            tracemalloc.stop()
        assert result["stale_candidates"] == count
        rss.append(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024))
        disk.append(result["sqlite_bytes"])
    assert peaks[1] < max(3 * peaks[0], 8 * 1024 * 1024)
    print(f"repair target index Python peak bytes (10k / 100k): {peaks}")
    print(f"process lifetime peak RSS bytes (10k / 100k): {rss}")
    print(f"repair SQLite main file bytes (10k / 100k): {disk}")
