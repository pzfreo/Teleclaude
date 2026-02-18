"""UK train times tools via National Rail Darwin OpenLDBWS (SOAP API)."""

import json
import logging
from typing import Any

from zeep import Client, Settings, xsd

from station_codes import search_stations as _search_stations

logger = logging.getLogger(__name__)

WSDL = "https://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01"
TOKEN_NAMESPACE = "http://thalesgroup.com/RTTI/2013-11-28/Token/types"


def _attr(obj: Any, name: str, default: Any = "") -> Any:
    """Safely get an attribute from a zeep object, returning default if None or missing."""
    if obj is None:
        return default
    val = getattr(obj, name, default)
    return default if val is None else val


class TrainClient:
    """Client for the National Rail Darwin OpenLDBWS SOAP API."""

    def __init__(self, token: str):
        settings = Settings(strict=False)
        self._client = Client(wsdl=WSDL, settings=settings)
        header_element = xsd.Element(
            f"{{{TOKEN_NAMESPACE}}}AccessToken",
            xsd.ComplexType(
                [
                    xsd.Element(f"{{{TOKEN_NAMESPACE}}}TokenValue", xsd.String()),
                ]
            ),
        )
        self._header = header_element(TokenValue=token)

    def _call(self, method: str, **kwargs: Any) -> Any:
        """Call an OpenLDBWS operation with the auth header."""
        operation = getattr(self._client.service, method)
        return operation(**kwargs, _soapheaders=[self._header])

    def get_departures(
        self,
        station: str,
        num_rows: int = 10,
        filter_station: str | None = None,
        time_offset: int | None = None,
        time_window: int | None = None,
    ) -> dict:
        """Get departures from a station."""
        kwargs: dict[str, Any] = {"numRows": num_rows, "crs": station}
        if filter_station:
            kwargs["filterCrs"] = filter_station
            kwargs["filterType"] = "to"
        if time_offset is not None:
            kwargs["timeOffset"] = time_offset
        if time_window is not None:
            kwargs["timeWindow"] = time_window
        res = self._call("GetDepartureBoard", **kwargs)
        return self._format_board(res, "departures")

    def get_arrivals(
        self,
        station: str,
        num_rows: int = 10,
        filter_station: str | None = None,
        time_offset: int | None = None,
        time_window: int | None = None,
    ) -> dict:
        """Get arrivals at a station."""
        kwargs: dict[str, Any] = {"numRows": num_rows, "crs": station}
        if filter_station:
            kwargs["filterCrs"] = filter_station
            kwargs["filterType"] = "from"
        if time_offset is not None:
            kwargs["timeOffset"] = time_offset
        if time_window is not None:
            kwargs["timeWindow"] = time_window
        res = self._call("GetArrivalBoard", **kwargs)
        return self._format_board(res, "arrivals")

    def search_stations(self, query: str) -> list[dict]:
        """Search for station names and CRS codes."""
        return _search_stations(query)

    def get_service_details(self, service_id: str) -> dict:
        """Get details for a specific service by its ID."""
        res = self._call("GetServiceDetails", serviceID=service_id)
        calling_points = []
        for cp_list in _attr(res, "subsequentCallingPoints", None) or []:
            for cp in _attr(cp_list, "callingPoint", None) or []:
                calling_points.append(
                    {
                        "station": _attr(cp, "locationName", ""),
                        "crs": _attr(cp, "crs", ""),
                        "scheduled": _attr(cp, "st", ""),
                        "expected": _attr(cp, "et", ""),
                    }
                )
        previous_points = []
        for cp_list in _attr(res, "previousCallingPoints", None) or []:
            for cp in _attr(cp_list, "callingPoint", None) or []:
                previous_points.append(
                    {
                        "station": _attr(cp, "locationName", ""),
                        "crs": _attr(cp, "crs", ""),
                        "scheduled": _attr(cp, "st", ""),
                        "expected": _attr(cp, "et", ""),
                    }
                )
        return {
            "operator": _attr(res, "operator", ""),
            "is_cancelled": _attr(res, "isCancelled", False),
            "cancel_reason": _attr(res, "cancelReason"),
            "delay_reason": _attr(res, "delayReason"),
            "platform": _attr(res, "platform"),
            "scheduled_departure": _attr(res, "std"),
            "estimated_departure": _attr(res, "etd"),
            "scheduled_arrival": _attr(res, "sta"),
            "estimated_arrival": _attr(res, "eta"),
            "previous_calling_points": previous_points,
            "subsequent_calling_points": calling_points,
        }

    def _format_board(self, res: Any, board_type: str) -> dict:
        """Format a departure/arrival board response from SOAP response object."""
        services_container = _attr(res, "trainServices", None)
        services = _attr(services_container, "service", []) if services_container else []
        formatted = []
        for svc in services:
            origin_container = _attr(svc, "origin", None)
            dest_container = _attr(svc, "destination", None)
            origins = [_attr(loc, "locationName", "") for loc in (_attr(origin_container, "location", []))]
            destinations = [_attr(loc, "locationName", "") for loc in (_attr(dest_container, "location", []))]
            entry: dict[str, str | bool | None] = {
                "origin": ", ".join(origins),
                "destination": ", ".join(destinations),
                "platform": _attr(svc, "platform", None),
                "operator": _attr(svc, "operator", ""),
                "service_id": _attr(svc, "serviceID", ""),
                "is_cancelled": _attr(svc, "isCancelled", False),
                "cancel_reason": _attr(svc, "cancelReason", None),
                "delay_reason": _attr(svc, "delayReason", None),
            }
            if board_type == "departures":
                entry["scheduled"] = _attr(svc, "std", "")
                entry["expected"] = _attr(svc, "etd", "")
            else:
                entry["scheduled"] = _attr(svc, "sta", "")
                entry["expected"] = _attr(svc, "eta", "")
            formatted.append(entry)

        result: dict = {
            "station": _attr(res, "locationName", ""),
            "crs": _attr(res, "crs", ""),
            "generated_at": str(_attr(res, "generatedAt", "")),
            "services": formatted,
        }
        nrcc = _attr(res, "nrccMessages", None)
        if nrcc:
            messages = _attr(nrcc, "message", [])
            result["notices"] = [_attr(m, "_value_1", str(m)) for m in messages]
        return result


TRAIN_TOOLS = [
    {
        "name": "get_train_departures",
        "description": (
            "Get live train departures from a UK station. Use CRS codes (3 letters, e.g. CHI=Chichester, "
            "BAA=Barnham, VIC=London Victoria, WAT=London Waterloo, CLJ=Clapham Junction). "
            "Use search_stations to find CRS codes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "station": {
                    "type": "string",
                    "description": "Station CRS code (e.g. 'BAA' for Barnham)",
                },
                "num_rows": {
                    "type": "integer",
                    "description": "Number of departures to return (default 10, max 150)",
                    "default": 10,
                },
                "filter_station": {
                    "type": "string",
                    "description": "Only show trains calling at this station (CRS code)",
                },
                "time_offset": {
                    "type": "integer",
                    "description": "Offset in minutes from now (0-119)",
                },
                "time_window": {
                    "type": "integer",
                    "description": "Window in minutes to search (0-120)",
                },
            },
            "required": ["station"],
        },
    },
    {
        "name": "get_train_arrivals",
        "description": (
            "Get live train arrivals at a UK station. Use CRS codes (3 letters). "
            "Use search_stations to find CRS codes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "station": {
                    "type": "string",
                    "description": "Station CRS code (e.g. 'BAA' for Barnham)",
                },
                "num_rows": {
                    "type": "integer",
                    "description": "Number of arrivals to return (default 10, max 150)",
                    "default": 10,
                },
                "filter_station": {
                    "type": "string",
                    "description": "Only show trains coming from this station (CRS code)",
                },
                "time_offset": {
                    "type": "integer",
                    "description": "Offset in minutes from now (0-119)",
                },
                "time_window": {
                    "type": "integer",
                    "description": "Window in minutes to search (0-120)",
                },
            },
            "required": ["station"],
        },
    },
    {
        "name": "search_stations",
        "description": "Search for UK railway station names and their CRS codes. Use this to find the correct station code before looking up departures/arrivals.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Station name or partial name to search for",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_service_details",
        "description": "Get full details of a specific train service including all calling points. Use a service_id from departures/arrivals results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_id": {
                    "type": "string",
                    "description": "The service ID from a departures or arrivals result",
                },
            },
            "required": ["service_id"],
        },
    },
]


def execute_tool(client: TrainClient, tool_name: str, tool_input: dict) -> str:
    """Execute a train tool call and return the result as a string."""
    try:
        if tool_name == "get_train_departures":
            return json.dumps(
                client.get_departures(
                    station=tool_input["station"],
                    num_rows=min(tool_input.get("num_rows", 10), 150),
                    filter_station=tool_input.get("filter_station"),
                    time_offset=tool_input.get("time_offset"),
                    time_window=tool_input.get("time_window"),
                ),
                indent=2,
            )
        elif tool_name == "get_train_arrivals":
            return json.dumps(
                client.get_arrivals(
                    station=tool_input["station"],
                    num_rows=min(tool_input.get("num_rows", 10), 150),
                    filter_station=tool_input.get("filter_station"),
                    time_offset=tool_input.get("time_offset"),
                    time_window=tool_input.get("time_window"),
                ),
                indent=2,
            )
        elif tool_name == "search_stations":
            return json.dumps(client.search_stations(tool_input["query"]), indent=2)
        elif tool_name == "get_service_details":
            return json.dumps(client.get_service_details(tool_input["service_id"]), indent=2)
        return f"Unknown tool: {tool_name}"
    except Exception as e:
        return f"Train times error: {e}"
