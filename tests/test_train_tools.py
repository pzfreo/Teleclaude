"""Tests for train_tools.py."""

import json
from unittest.mock import MagicMock, patch

MOCK_DEPARTURES_RESPONSE = {
    "trainServices": [
        {
            "origin": [{"locationName": "London Victoria", "crs": "VIC"}],
            "destination": [{"locationName": "Bognor Regis", "crs": "BOG"}],
            "std": "16:11",
            "etd": "On time",
            "platform": "17",
            "operator": "Southern",
            "operatorCode": "SN",
            "isCancelled": False,
            "cancelReason": None,
            "delayReason": None,
            "serviceID": "abc123==",
        },
        {
            "origin": [{"locationName": "London Victoria", "crs": "VIC"}],
            "destination": [{"locationName": "Chichester", "crs": "CHI"}],
            "std": "16:26",
            "etd": "16:31",
            "platform": "15",
            "operator": "Southern",
            "operatorCode": "SN",
            "isCancelled": False,
            "cancelReason": None,
            "delayReason": "Late running of a previous service",
            "serviceID": "def456==",
        },
    ],
    "busServices": None,
    "ferryServices": None,
    "locationName": "Barnham",
    "crs": "BAA",
    "generatedAt": "2026-02-15T16:00:00.0000000+00:00",
    "nrccMessages": None,
    "platformAvailable": True,
    "areServicesAvailable": True,
}

MOCK_ARRIVALS_RESPONSE = {
    "trainServices": [
        {
            "origin": [{"locationName": "Bognor Regis", "crs": "BOG"}],
            "destination": [{"locationName": "London Victoria", "crs": "VIC"}],
            "sta": "16:20",
            "eta": "On time",
            "platform": "2",
            "operator": "Southern",
            "operatorCode": "SN",
            "isCancelled": False,
            "cancelReason": None,
            "delayReason": None,
            "serviceID": "ghi789==",
        },
    ],
    "locationName": "Barnham",
    "crs": "BAA",
    "generatedAt": "2026-02-15T16:00:00.0000000+00:00",
    "nrccMessages": [{"value": "Disruption between Horsham and Barnham."}],
    "platformAvailable": True,
    "areServicesAvailable": True,
}

MOCK_SERVICE_RESPONSE = {
    "operator": "Southern",
    "isCancelled": False,
    "cancelReason": None,
    "delayReason": None,
    "platform": "17",
    "std": "16:11",
    "etd": "On time",
    "sta": None,
    "eta": None,
    "previousCallingPoints": [
        {
            "callingPoint": [
                {"locationName": "London Victoria", "crs": "VIC", "st": "16:11", "et": "On time"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "16:19", "et": "On time"},
            ]
        }
    ],
    "subsequentCallingPoints": [
        {
            "callingPoint": [
                {"locationName": "Barnham", "crs": "BAA", "st": "17:22", "et": "On time"},
                {"locationName": "Bognor Regis", "crs": "BOG", "st": "17:30", "et": "On time"},
            ]
        }
    ],
}

MOCK_STATION_SEARCH = [
    {"stationName": "Barnham", "crsCode": "BAA"},
]


class TestTrainClient:
    def test_get_departures(self):
        from train_tools import TrainClient

        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = MOCK_DEPARTURES_RESPONSE
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            result = client.get_departures("BAA", num_rows=5)

        assert result["station"] == "Barnham"
        assert result["crs"] == "BAA"
        assert len(result["services"]) == 2
        assert result["services"][0]["scheduled"] == "16:11"
        assert result["services"][0]["expected"] == "On time"
        assert result["services"][0]["destination"] == "Bognor Regis"
        assert result["services"][0]["platform"] == "17"
        assert result["services"][1]["delay_reason"] == "Late running of a previous service"
        mock_get.assert_called_once()
        assert "/departures/BAA/5" in mock_get.call_args[0][0]

    def test_get_departures_with_filter(self):
        from train_tools import TrainClient

        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = MOCK_DEPARTURES_RESPONSE
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            client.get_departures("VIC", filter_station="BAA", num_rows=10)

        url = mock_get.call_args[0][0]
        assert "/departures/VIC/to/BAA/10" in url

    def test_get_departures_with_time_params(self):
        from train_tools import TrainClient

        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = MOCK_DEPARTURES_RESPONSE
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            client.get_departures("BAA", time_offset=30, time_window=60)

        url = mock_get.call_args[0][0]
        assert "timeOffset=30" in url
        assert "timeWindow=60" in url

    def test_get_arrivals(self):
        from train_tools import TrainClient

        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = MOCK_ARRIVALS_RESPONSE
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            result = client.get_arrivals("BAA")

        assert result["station"] == "Barnham"
        assert len(result["services"]) == 1
        assert result["services"][0]["scheduled"] == "16:20"
        assert result["services"][0]["expected"] == "On time"
        assert result["services"][0]["origin"] == "Bognor Regis"
        assert "notices" in result
        assert "Disruption" in result["notices"][0]
        mock_get.assert_called_once()
        assert "/arrivals/BAA/10" in mock_get.call_args[0][0]

    def test_get_arrivals_with_filter(self):
        from train_tools import TrainClient

        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = MOCK_ARRIVALS_RESPONSE
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            client.get_arrivals("BAA", filter_station="VIC")

        url = mock_get.call_args[0][0]
        assert "/arrivals/BAA/from/VIC/10" in url

    def test_search_stations(self):
        from train_tools import TrainClient

        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = MOCK_STATION_SEARCH
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            result = client.search_stations("barnham")

        assert len(result) == 1
        assert result[0]["station_name"] == "Barnham"
        assert result[0]["crs_code"] == "BAA"

    def test_get_service_details(self):
        from train_tools import TrainClient

        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = MOCK_SERVICE_RESPONSE
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            result = client.get_service_details("abc123==")

        assert result["operator"] == "Southern"
        assert result["is_cancelled"] is False
        assert len(result["previous_calling_points"]) == 2
        assert result["previous_calling_points"][0]["station"] == "London Victoria"
        assert len(result["subsequent_calling_points"]) == 2
        assert result["subsequent_calling_points"][1]["station"] == "Bognor Regis"

    def test_no_services(self):
        from train_tools import TrainClient

        empty_response = {
            "trainServices": None,
            "locationName": "Barnham",
            "crs": "BAA",
            "generatedAt": "2026-02-15T02:00:00.0000000+00:00",
            "nrccMessages": None,
            "platformAvailable": True,
            "areServicesAvailable": True,
        }
        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = empty_response
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            result = client.get_departures("BAA")

        assert result["services"] == []

    def test_cancelled_service(self):
        from train_tools import TrainClient

        cancelled_response = {
            "trainServices": [
                {
                    "origin": [{"locationName": "London Victoria", "crs": "VIC"}],
                    "destination": [{"locationName": "Bognor Regis", "crs": "BOG"}],
                    "std": "16:11",
                    "etd": "Cancelled",
                    "platform": None,
                    "operator": "Southern",
                    "operatorCode": "SN",
                    "isCancelled": True,
                    "cancelReason": "A fault with the signalling system",
                    "delayReason": None,
                    "serviceID": "xyz999==",
                },
            ],
            "locationName": "Barnham",
            "crs": "BAA",
            "generatedAt": "2026-02-15T16:00:00.0000000+00:00",
            "nrccMessages": None,
            "platformAvailable": True,
            "areServicesAvailable": True,
        }
        with patch("train_tools.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.return_value = cancelled_response
            mock_get.return_value.raise_for_status = MagicMock()

            client = TrainClient()
            result = client.get_departures("BAA")

        svc = result["services"][0]
        assert svc["is_cancelled"] is True
        assert svc["cancel_reason"] == "A fault with the signalling system"
        assert svc["expected"] == "Cancelled"


class TestExecuteTool:
    def test_get_train_departures(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        client.get_departures.return_value = {"station": "Barnham", "services": []}
        result = execute_tool(client, "get_train_departures", {"station": "BAA"})
        parsed = json.loads(result)
        assert parsed["station"] == "Barnham"
        client.get_departures.assert_called_once_with(
            station="BAA", num_rows=10, filter_station=None, time_offset=None, time_window=None
        )

    def test_get_train_departures_with_options(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        client.get_departures.return_value = {"station": "VIC", "services": []}
        execute_tool(
            client,
            "get_train_departures",
            {"station": "VIC", "num_rows": 5, "filter_station": "BAA", "time_offset": 30},
        )
        client.get_departures.assert_called_once_with(
            station="VIC", num_rows=5, filter_station="BAA", time_offset=30, time_window=None
        )

    def test_num_rows_capped_at_150(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        client.get_departures.return_value = {"station": "BAA", "services": []}
        execute_tool(client, "get_train_departures", {"station": "BAA", "num_rows": 999})
        client.get_departures.assert_called_once_with(
            station="BAA", num_rows=150, filter_station=None, time_offset=None, time_window=None
        )

    def test_get_train_arrivals(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        client.get_arrivals.return_value = {"station": "BAA", "services": []}
        result = execute_tool(client, "get_train_arrivals", {"station": "BAA"})
        parsed = json.loads(result)
        assert parsed["station"] == "BAA"
        client.get_arrivals.assert_called_once()

    def test_search_stations(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        client.search_stations.return_value = [{"station_name": "Barnham", "crs_code": "BAA"}]
        result = execute_tool(client, "search_stations", {"query": "barnham"})
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["crs_code"] == "BAA"

    def test_get_service_details(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        client.get_service_details.return_value = {"operator": "Southern", "is_cancelled": False}
        result = execute_tool(client, "get_service_details", {"service_id": "abc123=="})
        parsed = json.loads(result)
        assert parsed["operator"] == "Southern"

    def test_unknown_tool(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        result = execute_tool(client, "nonexistent_tool", {})
        assert "Unknown tool" in result

    def test_error_handling(self):
        from train_tools import TrainClient, execute_tool

        client = MagicMock(spec=TrainClient)
        client.get_departures.side_effect = RuntimeError("Network error")
        result = execute_tool(client, "get_train_departures", {"station": "BAA"})
        assert "Train times error" in result


class TestToolDefinitions:
    def test_tool_schemas_are_valid(self):
        from train_tools import TRAIN_TOOLS

        assert len(TRAIN_TOOLS) == 4
        names = {t["name"] for t in TRAIN_TOOLS}
        assert names == {"get_train_departures", "get_train_arrivals", "search_stations", "get_service_details"}
        for tool in TRAIN_TOOLS:
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"]["type"] == "object"
            assert "required" in tool["input_schema"]
