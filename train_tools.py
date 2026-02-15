"""UK train times tools via Huxley2 (National Rail Darwin wrapper). No API key needed."""

import json
import logging

import requests

logger = logging.getLogger(__name__)

HUXLEY2_BASE = "https://huxley2.azurewebsites.net"
REQUEST_TIMEOUT = 15


class TrainClient:
    """Client for the Huxley2 UK train times API."""

    def __init__(self, base_url: str = HUXLEY2_BASE):
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str) -> dict | list:
        """Make a GET request to the Huxley2 API."""
        url = f"{self.base_url}{path}"
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def get_departures(
        self,
        station: str,
        num_rows: int = 10,
        filter_station: str | None = None,
        time_offset: int | None = None,
        time_window: int | None = None,
    ) -> dict:
        """Get departures from a station."""
        path = f"/departures/{station}/{num_rows}"
        if filter_station:
            path = f"/departures/{station}/to/{filter_station}/{num_rows}"
        params = []
        if time_offset is not None:
            params.append(f"timeOffset={time_offset}")
        if time_window is not None:
            params.append(f"timeWindow={time_window}")
        if params:
            path += "?" + "&".join(params)

        data = self._get(path)
        if not isinstance(data, dict):
            return {"error": "Unexpected response format"}
        return self._format_board(data, "departures")

    def get_arrivals(
        self,
        station: str,
        num_rows: int = 10,
        filter_station: str | None = None,
        time_offset: int | None = None,
        time_window: int | None = None,
    ) -> dict:
        """Get arrivals at a station."""
        path = f"/arrivals/{station}/{num_rows}"
        if filter_station:
            path = f"/arrivals/{station}/from/{filter_station}/{num_rows}"
        params = []
        if time_offset is not None:
            params.append(f"timeOffset={time_offset}")
        if time_window is not None:
            params.append(f"timeWindow={time_window}")
        if params:
            path += "?" + "&".join(params)

        data = self._get(path)
        if not isinstance(data, dict):
            return {"error": "Unexpected response format"}
        return self._format_board(data, "arrivals")

    def search_stations(self, query: str) -> list[dict]:
        """Search for station names and CRS codes."""
        data = self._get(f"/crs/{query}")
        if not isinstance(data, list):
            return []
        return [{"station_name": s.get("stationName", ""), "crs_code": s.get("crsCode", "")} for s in data]

    def get_service_details(self, service_id: str) -> dict:
        """Get details for a specific service by its ID."""
        data = self._get(f"/service/{service_id}")
        if not isinstance(data, dict):
            return {"error": "Unexpected response format"}
        calling_points = []
        for cp_list in data.get("subsequentCallingPoints", []) or []:
            for cp in cp_list.get("callingPoint", []) or []:
                calling_points.append(
                    {
                        "station": cp.get("locationName", ""),
                        "crs": cp.get("crs", ""),
                        "scheduled": cp.get("st", ""),
                        "expected": cp.get("et", ""),
                    }
                )
        previous_points = []
        for cp_list in data.get("previousCallingPoints", []) or []:
            for cp in cp_list.get("callingPoint", []) or []:
                previous_points.append(
                    {
                        "station": cp.get("locationName", ""),
                        "crs": cp.get("crs", ""),
                        "scheduled": cp.get("st", ""),
                        "expected": cp.get("et", ""),
                    }
                )
        return {
            "operator": data.get("operator", ""),
            "is_cancelled": data.get("isCancelled", False),
            "cancel_reason": data.get("cancelReason"),
            "delay_reason": data.get("delayReason"),
            "platform": data.get("platform"),
            "scheduled_departure": data.get("std"),
            "estimated_departure": data.get("etd"),
            "scheduled_arrival": data.get("sta"),
            "estimated_arrival": data.get("eta"),
            "previous_calling_points": previous_points,
            "subsequent_calling_points": calling_points,
        }

    def _format_board(self, data: dict, board_type: str) -> dict:
        """Format a departure/arrival board response."""
        services = data.get("trainServices") or []
        formatted = []
        for svc in services:
            origins = [o.get("locationName", "") for o in (svc.get("origin") or [])]
            destinations = [d.get("locationName", "") for d in (svc.get("destination") or [])]
            entry: dict[str, str | bool | None] = {
                "origin": ", ".join(origins),
                "destination": ", ".join(destinations),
                "platform": svc.get("platform"),
                "operator": svc.get("operator", ""),
                "service_id": svc.get("serviceID", ""),
                "is_cancelled": svc.get("isCancelled", False),
                "cancel_reason": svc.get("cancelReason"),
                "delay_reason": svc.get("delayReason"),
            }
            if board_type == "departures":
                entry["scheduled"] = svc.get("std", "")
                entry["expected"] = svc.get("etd", "")
            else:
                entry["scheduled"] = svc.get("sta", "")
                entry["expected"] = svc.get("eta", "")
            formatted.append(entry)

        result: dict = {
            "station": data.get("locationName", ""),
            "crs": data.get("crs", ""),
            "generated_at": data.get("generatedAt", ""),
            "services": formatted,
        }
        messages = data.get("nrccMessages") or []
        if messages:
            result["notices"] = [m.get("value", "") for m in messages]
        return result


TRAIN_TOOLS = [
    {
        "name": "get_train_departures",
        "description": (
            "Get live train departures from a UK station. Use CRS codes (3 letters, e.g. CHI=Chichester, "
            "BAA=Barnham, VIC=London Victoria, WAT=London Waterloo, CLJ=Clapham Junction) or station names. "
            "Use search_stations to find CRS codes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "station": {
                    "type": "string",
                    "description": "Station CRS code or name (e.g. 'BAA' or 'Barnham')",
                },
                "num_rows": {
                    "type": "integer",
                    "description": "Number of departures to return (default 10, max 150)",
                    "default": 10,
                },
                "filter_station": {
                    "type": "string",
                    "description": "Only show trains calling at this station (CRS code or name)",
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
            "Get live train arrivals at a UK station. Use CRS codes (3 letters) or station names. "
            "Use search_stations to find CRS codes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "station": {
                    "type": "string",
                    "description": "Station CRS code or name (e.g. 'BAA' or 'Barnham')",
                },
                "num_rows": {
                    "type": "integer",
                    "description": "Number of arrivals to return (default 10, max 150)",
                    "default": 10,
                },
                "filter_station": {
                    "type": "string",
                    "description": "Only show trains coming from this station (CRS code or name)",
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
