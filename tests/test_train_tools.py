"""Tests for train_tools.py (Darwin OpenLDBWS via zeep)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _loc(name, crs):
    """Create a mock ServiceLocation object."""
    return SimpleNamespace(locationName=name, crs=crs)


def _svc(
    std="",
    etd="",
    sta=None,
    eta=None,
    platform=None,
    operator="",
    service_id="",
    is_cancelled=False,
    cancel_reason=None,
    delay_reason=None,
    origins=None,
    destinations=None,
):
    """Create a mock ServiceItem object."""
    return SimpleNamespace(
        std=std,
        etd=etd,
        sta=sta,
        eta=eta,
        platform=platform,
        operator=operator,
        serviceID=service_id,
        isCancelled=is_cancelled,
        cancelReason=cancel_reason,
        delayReason=delay_reason,
        origin=SimpleNamespace(location=origins or []),
        destination=SimpleNamespace(location=destinations or []),
    )


def _board(location_name, crs, generated_at, services=None, nrcc=None):
    """Create a mock StationBoard response."""
    train_services = SimpleNamespace(service=services) if services else None
    return SimpleNamespace(
        locationName=location_name,
        crs=crs,
        generatedAt=generated_at,
        trainServices=train_services,
        nrccMessages=nrcc,
    )


MOCK_DEPARTURES_RESPONSE = _board(
    "Barnham",
    "BAA",
    "2026-02-15T16:00:00",
    services=[
        _svc(
            std="16:11",
            etd="On time",
            platform="17",
            operator="Southern",
            service_id="abc123==",
            origins=[_loc("London Victoria", "VIC")],
            destinations=[_loc("Bognor Regis", "BOG")],
        ),
        _svc(
            std="16:26",
            etd="16:31",
            platform="15",
            operator="Southern",
            service_id="def456==",
            delay_reason="Late running of a previous service",
            origins=[_loc("London Victoria", "VIC")],
            destinations=[_loc("Chichester", "CHI")],
        ),
    ],
)

MOCK_ARRIVALS_RESPONSE = _board(
    "Barnham",
    "BAA",
    "2026-02-15T16:00:00",
    services=[
        _svc(
            sta="16:20",
            eta="On time",
            platform="2",
            operator="Southern",
            service_id="ghi789==",
            origins=[_loc("Bognor Regis", "BOG")],
            destinations=[_loc("London Victoria", "VIC")],
        ),
    ],
    nrcc=SimpleNamespace(message=[SimpleNamespace(_value_1="Disruption between Horsham and Barnham.")]),
)


def _cp(name, crs, st, et):
    """Create a mock CallingPoint object."""
    return SimpleNamespace(locationName=name, crs=crs, st=st, et=et)


MOCK_SERVICE_RESPONSE = SimpleNamespace(
    operator="Southern",
    isCancelled=False,
    cancelReason=None,
    delayReason=None,
    platform="17",
    std="16:11",
    etd="On time",
    sta=None,
    eta=None,
    previousCallingPoints=[
        SimpleNamespace(
            callingPoint=[
                _cp("London Victoria", "VIC", "16:11", "On time"),
                _cp("Clapham Junction", "CLJ", "16:19", "On time"),
            ]
        )
    ],
    subsequentCallingPoints=[
        SimpleNamespace(
            callingPoint=[
                _cp("Barnham", "BAA", "17:22", "On time"),
                _cp("Bognor Regis", "BOG", "17:30", "On time"),
            ]
        )
    ],
)


@patch("train_tools.Client")
class TestTrainClient:
    def test_get_departures(self, MockClient):
        from train_tools import TrainClient

        MockClient.return_value.service.GetDepartureBoard.return_value = MOCK_DEPARTURES_RESPONSE
        client = TrainClient("test-token")
        result = client.get_departures("BAA", num_rows=5)

        assert result["station"] == "Barnham"
        assert result["crs"] == "BAA"
        assert len(result["services"]) == 2
        assert result["services"][0]["scheduled"] == "16:11"
        assert result["services"][0]["expected"] == "On time"
        assert result["services"][0]["destination"] == "Bognor Regis"
        assert result["services"][0]["platform"] == "17"
        assert result["services"][1]["delay_reason"] == "Late running of a previous service"
        call_kwargs = MockClient.return_value.service.GetDepartureBoard.call_args[1]
        assert call_kwargs["crs"] == "BAA"
        assert call_kwargs["numRows"] == 5

    def test_get_departures_with_filter(self, MockClient):
        from train_tools import TrainClient

        MockClient.return_value.service.GetDepartureBoard.return_value = MOCK_DEPARTURES_RESPONSE
        client = TrainClient("test-token")
        client.get_departures("VIC", filter_station="BAA", num_rows=10)

        call_kwargs = MockClient.return_value.service.GetDepartureBoard.call_args[1]
        assert call_kwargs["crs"] == "VIC"
        assert call_kwargs["filterCrs"] == "BAA"
        assert call_kwargs["filterType"] == "to"

    def test_get_departures_with_time_params(self, MockClient):
        from train_tools import TrainClient

        MockClient.return_value.service.GetDepartureBoard.return_value = MOCK_DEPARTURES_RESPONSE
        client = TrainClient("test-token")
        client.get_departures("BAA", time_offset=30, time_window=60)

        call_kwargs = MockClient.return_value.service.GetDepartureBoard.call_args[1]
        assert call_kwargs["timeOffset"] == 30
        assert call_kwargs["timeWindow"] == 60

    def test_get_arrivals(self, MockClient):
        from train_tools import TrainClient

        MockClient.return_value.service.GetArrivalBoard.return_value = MOCK_ARRIVALS_RESPONSE
        client = TrainClient("test-token")
        result = client.get_arrivals("BAA")

        assert result["station"] == "Barnham"
        assert len(result["services"]) == 1
        assert result["services"][0]["scheduled"] == "16:20"
        assert result["services"][0]["expected"] == "On time"
        assert result["services"][0]["origin"] == "Bognor Regis"
        assert "notices" in result
        assert "Disruption" in result["notices"][0]

    def test_get_arrivals_with_filter(self, MockClient):
        from train_tools import TrainClient

        MockClient.return_value.service.GetArrivalBoard.return_value = MOCK_ARRIVALS_RESPONSE
        client = TrainClient("test-token")
        client.get_arrivals("BAA", filter_station="VIC")

        call_kwargs = MockClient.return_value.service.GetArrivalBoard.call_args[1]
        assert call_kwargs["filterCrs"] == "VIC"
        assert call_kwargs["filterType"] == "from"

    def test_search_stations(self, MockClient):
        from train_tools import TrainClient

        client = TrainClient("test-token")
        result = client.search_stations("barnham")

        assert len(result) == 1
        assert result[0]["station_name"] == "Barnham"
        assert result[0]["crs_code"] == "BAA"

    def test_get_service_details(self, MockClient):
        from train_tools import TrainClient

        MockClient.return_value.service.GetServiceDetails.return_value = MOCK_SERVICE_RESPONSE
        client = TrainClient("test-token")
        result = client.get_service_details("abc123==")

        assert result["operator"] == "Southern"
        assert result["is_cancelled"] is False
        assert len(result["previous_calling_points"]) == 2
        assert result["previous_calling_points"][0]["station"] == "London Victoria"
        assert len(result["subsequent_calling_points"]) == 2
        assert result["subsequent_calling_points"][1]["station"] == "Bognor Regis"

    def test_no_services(self, MockClient):
        from train_tools import TrainClient

        empty_response = _board("Barnham", "BAA", "2026-02-15T02:00:00")
        MockClient.return_value.service.GetDepartureBoard.return_value = empty_response
        client = TrainClient("test-token")
        result = client.get_departures("BAA")

        assert result["services"] == []

    def test_cancelled_service(self, MockClient):
        from train_tools import TrainClient

        cancelled_response = _board(
            "Barnham",
            "BAA",
            "2026-02-15T16:00:00",
            services=[
                _svc(
                    std="16:11",
                    etd="Cancelled",
                    is_cancelled=True,
                    cancel_reason="A fault with the signalling system",
                    operator="Southern",
                    service_id="xyz999==",
                    origins=[_loc("London Victoria", "VIC")],
                    destinations=[_loc("Bognor Regis", "BOG")],
                ),
            ],
        )
        MockClient.return_value.service.GetDepartureBoard.return_value = cancelled_response
        client = TrainClient("test-token")
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
