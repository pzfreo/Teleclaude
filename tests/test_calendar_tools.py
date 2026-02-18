"""Tests for calendar_tools.py — Google Calendar client and tool dispatch."""

import json
from unittest.mock import MagicMock, patch


class TestGoogleCalendarClient:
    def _make_client(self):
        """Create a GoogleCalendarClient with mocked credentials."""
        with (
            patch("calendar_tools.Credentials") as mock_creds_cls,
            patch("calendar_tools.Request"),
            patch("calendar_tools.build") as mock_build,
        ):
            mock_creds = MagicMock()
            mock_creds_cls.return_value = mock_creds
            mock_service = MagicMock()
            mock_build.return_value = mock_service

            from calendar_tools import GoogleCalendarClient

            client = GoogleCalendarClient("cid", "csec", "rtok")
            return client, mock_service

    def test_list_events(self):
        client, svc = self._make_client()
        svc.events().list().execute.return_value = {
            "items": [
                {
                    "id": "e1",
                    "summary": "Meeting",
                    "start": {"dateTime": "2026-02-14T10:00:00Z"},
                    "end": {"dateTime": "2026-02-14T11:00:00Z"},
                    "location": "Office",
                    "description": "Weekly sync",
                }
            ]
        }
        events = client.list_events(days_ahead=7)
        assert len(events) == 1
        assert events[0]["summary"] == "Meeting"
        assert events[0]["location"] == "Office"

    def test_list_events_empty(self):
        client, svc = self._make_client()
        svc.events().list().execute.return_value = {"items": []}
        events = client.list_events()
        assert events == []

    def test_create_event(self):
        client, svc = self._make_client()
        svc.events().insert().execute.return_value = {
            "id": "new1",
            "summary": "Lunch",
            "start": {"dateTime": "2026-02-14T12:00:00"},
            "htmlLink": "https://calendar.google.com/event/new1",
        }
        result = client.create_event("Lunch", "2026-02-14T12:00:00", "2026-02-14T13:00:00")
        assert result["id"] == "new1"
        assert result["summary"] == "Lunch"

    def test_create_all_day_event(self):
        client, svc = self._make_client()
        svc.events().insert().execute.return_value = {
            "id": "ad1",
            "summary": "Holiday",
        }
        result = client.create_all_day_event("Holiday", "2026-02-14")
        assert result["id"] == "ad1"

    def test_delete_event(self):
        client, svc = self._make_client()
        svc.events().delete().execute.return_value = None
        result = client.delete_event("e1")
        assert result == "Event deleted."

    def test_list_calendars(self):
        client, svc = self._make_client()
        svc.calendarList().list().execute.return_value = {
            "items": [
                {"id": "primary", "summary": "My Calendar", "primary": True},
                {"id": "work", "summary": "Work", "primary": False},
            ]
        }
        cals = client.list_calendars()
        assert len(cals) == 2
        assert cals[0]["primary"] is True


class TestExecuteTool:
    def test_list_calendar_events(self):
        from calendar_tools import execute_tool

        client = MagicMock()
        client.list_events.return_value = [{"id": "1", "summary": "Test"}]
        result = execute_tool(client, "list_calendar_events", {"days_ahead": 3})
        parsed = json.loads(result)
        assert len(parsed) == 1
        client.list_events.assert_called_once_with(3, 250, "primary")

    def test_create_calendar_event(self):
        from calendar_tools import execute_tool

        client = MagicMock()
        client.create_event.return_value = {"id": "new", "summary": "Test"}
        result = execute_tool(
            client,
            "create_calendar_event",
            {"summary": "Test", "start": "2026-02-14T10:00:00", "end": "2026-02-14T11:00:00"},
        )
        parsed = json.loads(result)
        assert parsed["id"] == "new"

    def test_delete_calendar_event(self):
        from calendar_tools import execute_tool

        client = MagicMock()
        client.delete_event.return_value = "Event deleted."
        result = execute_tool(client, "delete_calendar_event", {"event_id": "e1"})
        assert result == "Event deleted."

    def test_unknown_tool(self):
        from calendar_tools import execute_tool

        client = MagicMock()
        result = execute_tool(client, "nonexistent_tool", {})
        assert "Unknown tool" in result

    def test_error_handling(self):
        from calendar_tools import execute_tool

        client = MagicMock()
        client.list_events.side_effect = RuntimeError("API down")
        result = execute_tool(client, "list_calendar_events", {})
        assert "Google Calendar error" in result
