"""Tests for station_codes.py."""

from station_codes import STATIONS, search_stations


class TestStationsData:
    def test_stations_dict_is_populated(self):
        assert len(STATIONS) > 2500

    def test_all_crs_codes_are_3_chars(self):
        for crs in STATIONS:
            assert len(crs) <= 3, f"CRS code '{crs}' is longer than 3 characters"

    def test_known_stations_exist(self):
        assert STATIONS["BAA"] == "Barnham"
        assert STATIONS["VIC"] == "London Victoria"
        assert STATIONS["WAT"] == "London Waterloo"
        assert STATIONS["EUS"] == "London Euston"
        assert STATIONS["CLJ"] == "Clapham Junction"


class TestSearchStations:
    def test_search_by_name(self):
        results = search_stations("barnham")
        assert any(r["crs_code"] == "BAA" for r in results)
        assert any(r["station_name"] == "Barnham" for r in results)

    def test_search_by_crs_code(self):
        results = search_stations("VIC")
        assert any(r["station_name"] == "London Victoria" for r in results)

    def test_search_case_insensitive(self):
        results_lower = search_stations("chichester")
        results_upper = search_stations("CHICHESTER")
        assert len(results_lower) > 0
        assert results_lower == results_upper

    def test_search_partial_match(self):
        results = search_stations("barn")
        names = [r["station_name"] for r in results]
        assert "Barnham" in names

    def test_search_empty_query(self):
        assert search_stations("") == []
        assert search_stations("   ") == []

    def test_search_no_results(self):
        assert search_stations("xyznonexistent") == []

    def test_search_max_results(self):
        results = search_stations("london")
        assert len(results) <= 20

    def test_search_result_format(self):
        results = search_stations("barnham")
        assert len(results) >= 1
        for r in results:
            assert "station_name" in r
            assert "crs_code" in r
