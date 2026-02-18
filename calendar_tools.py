"""Google Calendar tools that Claude can call via tool_use."""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+")


def _strip_urls(text: str) -> str:
    """Remove URLs from text to avoid Telegram link previews."""
    return _URL_RE.sub("", text).strip()


class GoogleCalendarClient:
    """Google Calendar API client using OAuth2 refresh token."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        creds.refresh(Request())
        self.service = build("calendar", "v3", credentials=creds)

    def _query_events(self, calendar_id: str, time_min: str, time_max: str, max_results: int) -> list[dict]:
        """Query events from a single calendar."""
        results = (
            self.service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        cal_name = results.get("summary", calendar_id)
        events = []
        for e in results.get("items", []):
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            end = e["end"].get("dateTime", e["end"].get("date", ""))
            # Strip URLs from location/description to avoid Telegram link previews
            location = e.get("location", "")
            description = e.get("description", "")
            if location:
                location = _strip_urls(location)
            if description:
                description = _strip_urls(description)
            event: dict[str, Any] = {
                "id": e["id"],
                "summary": e.get("summary", "(no title)"),
                "start": start,
                "end": end,
                "location": location,
                "description": description,
            }
            if calendar_id != "primary":
                event["calendar"] = cal_name
            events.append(event)
        return events

    def list_events(self, days_ahead: int = 7, max_results: int = 250, calendar_id: str = "all") -> list[dict]:
        """List upcoming events. Use calendar_id='all' to query all visible calendars."""
        now = datetime.now(tz=UTC)
        time_min = now.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        time_max = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"

        if calendar_id != "all":
            return self._query_events(calendar_id, time_min, time_max, max_results)

        # Query all visible calendars and merge
        calendars = self.list_calendars()
        all_events: list[dict] = []
        for cal in calendars:
            try:
                all_events.extend(self._query_events(cal["id"], time_min, time_max, max_results))
            except Exception as e:
                logger.warning("Failed to query calendar %s: %s", cal.get("summary", cal["id"]), e)
        # Sort by start time
        all_events.sort(key=lambda ev: ev.get("start", ""))
        return all_events

    def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        calendar_id: str = "primary",
    ) -> dict:
        """Create a calendar event. start/end in ISO 8601 format (e.g. 2026-02-10T14:00:00)."""
        body = {
            "summary": summary,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        result = self.service.events().insert(calendarId=calendar_id, body=body).execute()
        return {
            "id": result["id"],
            "summary": result.get("summary", ""),
            "start": result["start"].get("dateTime", ""),
            "link": result.get("htmlLink", ""),
        }

    def create_all_day_event(
        self,
        summary: str,
        date: str,
        description: str = "",
        calendar_id: str = "primary",
    ) -> dict:
        """Create an all-day event. date in YYYY-MM-DD format."""
        body = {
            "summary": summary,
            "start": {"date": date},
            "end": {"date": date},
        }
        if description:
            body["description"] = description

        result = self.service.events().insert(calendarId=calendar_id, body=body).execute()
        return {
            "id": result["id"],
            "summary": result.get("summary", ""),
            "date": date,
            "link": result.get("htmlLink", ""),
        }

    def delete_event(self, event_id: str, calendar_id: str = "primary") -> str:
        """Delete a calendar event."""
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return "Event deleted."

    def list_calendars(self) -> list[dict]:
        """List all calendars."""
        results = self.service.calendarList().list(maxResults=20).execute()
        return [
            {"id": c["id"], "summary": c.get("summary", ""), "primary": c.get("primary", False)}
            for c in results.get("items", [])
        ]


CALENDAR_TOOLS = [
    {
        "name": "list_calendar_events",
        "description": "List upcoming events from Google Calendar. By default queries all visible calendars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to look (default 7)",
                    "default": 7,
                },
                "max_results": {"type": "integer", "description": "Max events to return (default 250)", "default": 250},
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID, or 'all' to query all visible calendars (default)",
                    "default": "all",
                },
            },
            "required": [],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create a new calendar event with a specific start and end time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start time in ISO 8601 (e.g. 2026-02-10T14:00:00)"},
                "end": {"type": "string", "description": "End time in ISO 8601 (e.g. 2026-02-10T15:00:00)"},
                "description": {"type": "string", "description": "Event description", "default": ""},
                "location": {"type": "string", "description": "Event location", "default": ""},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["summary", "start", "end"],
        },
    },
    {
        "name": "create_all_day_event",
        "description": "Create an all-day calendar event.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                "description": {"type": "string", "default": ""},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["summary", "date"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "Delete a calendar event by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID of the event to delete"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "list_calendars",
        "description": "List all available Google Calendars.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def execute_tool(client: GoogleCalendarClient, tool_name: str, tool_input: dict) -> str:
    """Execute a Google Calendar tool call."""
    try:
        result: Any
        if tool_name == "list_calendar_events":
            result = client.list_events(
                tool_input.get("days_ahead", 7),
                tool_input.get("max_results", 250),
                tool_input.get("calendar_id", "all"),
            )
        elif tool_name == "create_calendar_event":
            result = client.create_event(
                tool_input["summary"],
                tool_input["start"],
                tool_input["end"],
                tool_input.get("description", ""),
                tool_input.get("location", ""),
                tool_input.get("calendar_id", "primary"),
            )
        elif tool_name == "create_all_day_event":
            result = client.create_all_day_event(
                tool_input["summary"],
                tool_input["date"],
                tool_input.get("description", ""),
                tool_input.get("calendar_id", "primary"),
            )
        elif tool_name == "delete_calendar_event":
            result = client.delete_event(
                tool_input["event_id"],
                tool_input.get("calendar_id", "primary"),
            )
        elif tool_name == "list_calendars":
            result = client.list_calendars()
        else:
            return f"Unknown tool: {tool_name}"

        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Google Calendar error: {e}"
