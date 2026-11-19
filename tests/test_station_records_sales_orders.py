from unittest.mock import Mock

from scripts.sync.station_records_sales_orders import load_production_orders


def test_load_production_orders_deduplicates_and_supports_legacy_shape():
    collection = Mock()
    collection.find.return_value = iter([
        {"data": {"AUFNR": " 1 "}},
        {"data": {"AUFNR": "1"}},
        {"aufnr": "2"},
        {"data": {}},
    ])
    assert load_production_orders(collection) == ["1", "2"]
    collection.find.assert_called_once_with({}, {"data.AUFNR": 1, "aufnr": 1})
