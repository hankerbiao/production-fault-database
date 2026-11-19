from scripts.sources.hana.hana_view_sync import normalized_station_order


def test_numeric_station_order_normalization():
    assert normalized_station_order("00000123") == "123"
    assert normalized_station_order("000") == "0"
    assert normalized_station_order(" 123 ") == "123"
    assert normalized_station_order("AB-123") is None
    assert normalized_station_order("") is None
    assert normalized_station_order(None) is None
