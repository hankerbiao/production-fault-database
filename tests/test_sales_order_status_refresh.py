from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.sync import sync_sales_orders as sales
from scripts.maintenance import refresh_sales_order_status as maintenance


def row(status="STARTED", *, line="10"):
    return {
        "MANDT": "800", "AUFNR": "100", "VBELN": "SO1",
        "POSNR": line, "ETENR": "1", "MATNR": "M1", "ZSTAT": status,
    }


def document(source="SG", rows=None):
    rows = deepcopy(rows if rows is not None else [row()])
    aufnr = rows[0]["AUFNR"]
    return {
        "_id": f"{source}:{aufnr}", "source": source, "aufnr": aufnr,
        "data": deepcopy(rows[-1]), "records": rows,
        "order_quantity": 10, "storage_quantity": 5,
    }


@pytest.fixture
def refresh(monkeypatch):
    monkeypatch.setattr(sales, "SOURCES", {"SG": "http://sg", "KK": "http://kk"})
    monkeypatch.setenv("TARGET_COLLECTION", "orders")
    monkeypatch.setenv("FULL_WINDOW_DAYS", "1")
    monkeypatch.setenv("SYNC_BATCH_SIZE", "1000")

    def run(documents, sg, kk=(), *, apply=True, second_sg=(), error=None,
            start=date(2026, 1, 1)):
        collection = Mock()
        collection.find.return_value = deepcopy(documents)
        recorded = []

        def write(operations, **kwargs):
            recorded.extend(operations)
            return SimpleNamespace(matched_count=len(operations))

        collection.bulk_write.side_effect = write
        db = Mock()
        db.__getitem__ = Mock(return_value=collection)
        responses = iter([list(sg), list(second_sg), list(kk), []])
        http = Mock()

        def post(url, **kwargs):
            if url == "http://kk" and error:
                raise error
            response = Mock()
            response.json.return_value = {"DATA": next(responses)}
            return response

        http.post.side_effect = post
        kwargs = dict(
            start=start, end=date(2026, 1, 2),
            prodh=["00100"], apply=apply, run_id="test-status",
        )
        return SimpleNamespace(
            execute=lambda: sales.refresh_order_statuses(db, http, **kwargs),
            db=db, collection=collection, http=http, operations=recorded,
        )

    return run


def writes(run):
    return run.operations


def test_patches_status_only_and_keeps_sources_separate(refresh):
    documents = [document("SG"), document("KK")]
    run = refresh(documents, [row("RELEASED")], [row("REL")])
    result = run.execute()
    assert result["success"] is True
    assert result["would_update"] == result["updated"] == result["checked"] == 2
    operations = {op._filter["_id"]: op for op in writes(run)}
    for identifier, status in [("SG:100", "RELEASED"), ("KK:100", "REL")]:
        operation = operations[identifier]
        assert operation._upsert is False
        update = operation._doc["$set"]
        assert update["data.ZSTAT"] == update["records.0.ZSTAT"] == status
        assert set(update) == {
            "data.ZSTAT", "records.0.ZSTAT", "_status_checked_at",
            "_status_changed_at", "_status_run_id",
        }
    assert documents[0]["data"]["ZSTAT"] == "STARTED"
    assert all(call.args == ("orders",) for call in run.db.__getitem__.call_args_list)


def test_matches_detail_identity_when_source_order_changes(refresh):
    run = refresh(
        [document(rows=[row(line="10"), row(line="20")])],
        [row("REL", line="20"), row("RELEASED", line="10")],
    )
    result = run.execute()
    assert result["success"] is True
    update = writes(run)[0]._doc["$set"]
    assert update["data.ZSTAT"] == "REL"
    assert update["records.0.ZSTAT"] == "RELEASED"
    assert update["records.1.ZSTAT"] == "REL"


def test_preview_does_not_write_statuses_or_metadata(refresh):
    run = refresh([document()], [row("REL")], apply=False)
    result = run.execute()
    assert result["success"] is True and result["dry_run"] is True
    assert result["would_update"] == 1 and result["updated"] == 0
    run.collection.bulk_write.assert_not_called()


def test_unchanged_status_only_updates_check_metadata(refresh):
    run = refresh([document()], [row()])
    result = run.execute()
    assert result["would_update"] == result["updated"] == 0
    assert result["unchanged"] == result["checked"] == 1
    assert set(writes(run)[0]._doc["$set"]) == {"_status_checked_at", "_status_run_id"}


@pytest.mark.parametrize("status", [None, "missing"])
def test_missing_status_preserves_order_and_reports_failure(refresh, status):
    source_row = row(status)
    if status == "missing":
        source_row.pop("ZSTAT")
    run = refresh([document()], [source_row])
    result = run.execute()
    assert result["success"] is False and result["missing_status"] == 1
    run.collection.bulk_write.assert_not_called()


def test_explicit_empty_status_is_applied(refresh):
    run = refresh([document()], [row("")])
    result = run.execute()
    assert result["success"] is True and result["updated"] == 1
    assert writes(run)[0]._doc["$set"]["data.ZSTAT"] == ""


def test_unreturned_order_is_reported_without_failing_the_selected_scope(refresh):
    run = refresh([document("SG"), document("KK")], [row()])
    result = run.execute()
    assert result["success"] is True and result["not_returned"] == 1
    assert result["scope"] == "returned_orders"
    assert result["not_returned_samples"] == ["KK:100"]
    assert result["checked"] == 1
    assert [op._filter["_id"] for op in writes(run)] == ["SG:100"]


@pytest.mark.parametrize("outside_fields", [
    {"GSTRS": "2025-12-01", "PRODH": "00100"},
    {"GSTRS": "2026-01-02", "PRODH": "00200"},
])
def test_source_membership_defines_scope_without_guessing_planned_dates(refresh, outside_fields):
    included = row("STARTED")
    included["GSTRS"] = "2025-01-01"
    outside = row("STARTED")
    outside.update(outside_fields, AUFNR="200")
    run = refresh(
        [document(rows=[included]), document(rows=[outside])],
        [{**included, "ZSTAT": "REL"}], start=date(2026, 1, 2),
    )
    result = run.execute()
    assert result["success"] is True
    assert result["existing"] == 2 and result["matched"] == 1
    assert result["not_returned"] == 1 and result["checked"] == 1
    assert len(writes(run)) == 1
    assert writes(run)[0]._doc["$set"]["data.ZSTAT"] == "REL"


def test_empty_valid_scope_reports_uncovered_orders_without_writes(refresh):
    run = refresh([document("SG"), document("KK")], [])
    result = run.execute()
    assert result["success"] is True
    assert result["not_returned"] == 2 and result["matched"] == result["checked"] == 0
    run.collection.bulk_write.assert_not_called()


def test_source_failure_does_not_write_first_source(refresh):
    run = refresh([document()], [row("REL")], error=RuntimeError("KK unavailable"))
    with pytest.raises(RuntimeError, match="KK unavailable"):
        run.execute()
    run.collection.bulk_write.assert_not_called()


def test_conflicting_duplicate_status_fails_before_writes(refresh):
    run = refresh([document()], [row("REL")], second_sg=[row("RELEASED")])
    with pytest.raises(RuntimeError, match="Conflicting source key"):
        run.execute()
    run.collection.bulk_write.assert_not_called()


def test_identical_duplicate_status_is_safe(refresh):
    run = refresh([document()], [row("REL")], second_sg=[row("REL")])
    assert run.execute()["updated"] == 1
    assert len(writes(run)) == 1


@pytest.mark.parametrize("source_row", [
    {"ZSTAT": "STARTED"}, row(3),
])
def test_invalid_source_row_fails_before_writes(refresh, source_row):
    run = refresh([document()], [source_row])
    with pytest.raises(RuntimeError):
        run.execute()
    run.collection.bulk_write.assert_not_called()


def test_unmatched_detail_is_not_overwritten(refresh):
    run = refresh([document()], [row("REL", line="20")])
    result = run.execute()
    assert result["success"] is False and result["unmatched_details"] == 1
    assert result["would_update"] == result["updated"] == result["checked"] == 0
    assert result["transitions"] == {}
    run.collection.bulk_write.assert_not_called()


@pytest.mark.parametrize("apply", [True, False])
def test_one_unmatched_detail_blocks_the_entire_order_update(refresh, apply):
    run = refresh(
        [document(rows=[row(line="10"), row(line="20")])],
        [row("REL", line="20")], apply=apply,
    )
    result = run.execute()
    assert result["success"] is False and result["unmatched_details"] == 1
    assert result["would_update"] == result["updated"] == result["checked"] == 0
    assert result["transitions"] == {}
    run.collection.bulk_write.assert_not_called()


def test_unmatched_representative_is_not_replaced_by_the_only_source_row(refresh):
    stored = document()
    stored["data"] = row(line="20")
    run = refresh([stored], [row("REL")])
    result = run.execute()
    assert result["success"] is False and result["missing_status"] == 1
    assert result["would_update"] == result["checked"] == 0
    run.collection.bulk_write.assert_not_called()


def test_missing_detail_status_blocks_preview_changes_and_transitions(refresh):
    run = refresh(
        [document(rows=[row(line="10"), row(line="20")])],
        [row(None, line="10"), row("REL", line="20")], apply=False,
    )
    result = run.execute()
    assert result["success"] is False and result["missing_status"] == 1
    assert result["would_update"] == 0 and result["transitions"] == {}
    run.collection.bulk_write.assert_not_called()


def test_incomplete_order_does_not_block_other_valid_orders(refresh, monkeypatch):
    monkeypatch.setenv("SYNC_BATCH_SIZE", "1")
    run = refresh(
        [document(rows=[row(line="10"), row(line="20")]), document("KK")],
        [row("REL", line="20")], [row("REL")],
    )
    result = run.execute()
    assert result["success"] is False and result["unmatched_details"] == 1
    assert result["would_update"] == result["updated"] == result["checked"] == 1
    assert result["transitions"] == {"STARTED -> REL": 1}
    assert [op._filter["_id"] for op in writes(run)] == ["KK:100"]


def test_status_refresh_uses_indexed_snapshot_reads(refresh, monkeypatch):
    class IndexedOnlySnapshot(sales.Snapshot):
        def __iter__(self):
            raise AssertionError("Status refresh must not load the entire snapshot")

    monkeypatch.setattr(sales, "Snapshot", IndexedOnlySnapshot)
    run = refresh([document()], [row("REL")])
    assert run.execute()["updated"] == 1


def test_temporary_script_defaults_to_preview():
    args = maintenance.build_parser().parse_args([])
    assert args.apply is False and args.start_date == date(2026, 1, 1)
    args = maintenance.build_parser().parse_args(["--apply", "--all-prodh"])
    assert args.apply is True and sales.configured_prodh(args) is None
