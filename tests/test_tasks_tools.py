"""Tests for tasks_tools.py — Google Tasks client and tool dispatch."""

import json
from unittest.mock import MagicMock, patch


class TestGoogleTasksClient:
    def _make_client(self):
        """Create a GoogleTasksClient with mocked credentials."""
        with (
            patch("tasks_tools.Credentials") as mock_creds_cls,
            patch("tasks_tools.Request"),
            patch("tasks_tools.build") as mock_build,
        ):
            mock_creds = MagicMock()
            mock_creds_cls.return_value = mock_creds
            mock_service = MagicMock()
            mock_build.return_value = mock_service

            from tasks_tools import GoogleTasksClient

            client = GoogleTasksClient("cid", "csec", "rtok")
            return client, mock_service

    def test_list_tasklists(self):
        client, svc = self._make_client()
        svc.tasklists().list().execute.return_value = {
            "items": [{"id": "tl1", "title": "My Tasks"}]
        }
        result = client.list_tasklists()
        assert len(result) == 1
        assert result[0]["title"] == "My Tasks"

    def test_list_tasks(self):
        client, svc = self._make_client()
        svc.tasks().list().execute.return_value = {
            "items": [
                {"id": "t1", "title": "Buy groceries", "notes": "", "status": "needsAction", "due": ""},
            ]
        }
        result = client.list_tasks()
        assert len(result) == 1
        assert result[0]["title"] == "Buy groceries"

    def test_create_task(self):
        client, svc = self._make_client()
        svc.tasks().insert().execute.return_value = {
            "id": "new1",
            "title": "New task",
            "status": "needsAction",
        }
        result = client.create_task("New task")
        assert result["id"] == "new1"

    def test_complete_task(self):
        client, svc = self._make_client()
        svc.tasks().get().execute.return_value = {
            "id": "t1",
            "title": "Task",
            "status": "needsAction",
        }
        svc.tasks().update().execute.return_value = {
            "id": "t1",
            "title": "Task",
            "status": "completed",
        }
        result = client.complete_task("t1")
        assert result["status"] == "completed"

    def test_delete_task(self):
        client, svc = self._make_client()
        svc.tasks().delete().execute.return_value = None
        result = client.delete_task("t1")
        assert result == "Task deleted."


class TestExecuteTool:
    def test_list_tasklists(self):
        from tasks_tools import execute_tool

        client = MagicMock()
        client.list_tasklists.return_value = [{"id": "tl1", "title": "My Tasks"}]
        result = execute_tool(client, "list_tasklists", {})
        parsed = json.loads(result)
        assert len(parsed) == 1

    def test_create_task(self):
        from tasks_tools import execute_tool

        client = MagicMock()
        client.create_task.return_value = {"id": "new", "title": "Test", "status": "needsAction"}
        result = execute_tool(client, "create_task", {"title": "Test"})
        parsed = json.loads(result)
        assert parsed["id"] == "new"

    def test_complete_task(self):
        from tasks_tools import execute_tool

        client = MagicMock()
        client.complete_task.return_value = {"id": "t1", "title": "Task", "status": "completed"}
        result = execute_tool(client, "complete_task", {"task_id": "t1"})
        parsed = json.loads(result)
        assert parsed["status"] == "completed"

    def test_unknown_tool(self):
        from tasks_tools import execute_tool

        client = MagicMock()
        result = execute_tool(client, "nonexistent", {})
        assert "Unknown tool" in result

    def test_error_handling(self):
        from tasks_tools import execute_tool

        client = MagicMock()
        client.list_tasklists.side_effect = RuntimeError("API failure")
        result = execute_tool(client, "list_tasklists", {})
        assert "Google Tasks error" in result
