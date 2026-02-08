"""Google Tasks tools that Claude can call via tool_use."""

import json
import logging
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


class GoogleTasksClient:
    """Google Tasks API client using OAuth2 refresh token."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        creds.refresh(Request())
        self.service = build("tasks", "v1", credentials=creds)

    def list_tasklists(self) -> list[dict]:
        """List all task lists."""
        results = self.service.tasklists().list(maxResults=20).execute()
        return [
            {"id": tl["id"], "title": tl["title"]}
            for tl in results.get("items", [])
        ]

    def list_tasks(self, tasklist_id: str = "@default", show_completed: bool = False) -> list[dict]:
        """List tasks in a task list."""
        results = (
            self.service.tasks()
            .list(tasklist=tasklist_id, showCompleted=show_completed, maxResults=100)
            .execute()
        )
        tasks = []
        for t in results.get("items", []):
            tasks.append(
                {
                    "id": t["id"],
                    "title": t.get("title", ""),
                    "notes": t.get("notes", ""),
                    "status": t.get("status", ""),
                    "due": t.get("due", ""),
                }
            )
        return tasks

    def create_task(
        self,
        title: str,
        notes: str = "",
        due: str = "",
        tasklist_id: str = "@default",
    ) -> dict:
        """Create a new task. Due date format: YYYY-MM-DD."""
        body: dict = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = f"{due}T00:00:00.000Z"
        result = self.service.tasks().insert(tasklist=tasklist_id, body=body).execute()
        return {"id": result["id"], "title": result["title"], "status": result.get("status", "")}

    def complete_task(self, task_id: str, tasklist_id: str = "@default") -> dict:
        """Mark a task as completed."""
        task = self.service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
        task["status"] = "completed"
        result = self.service.tasks().update(tasklist=tasklist_id, task=task_id, body=task).execute()
        return {"id": result["id"], "title": result["title"], "status": result["status"]}

    def update_task(
        self,
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
        tasklist_id: str = "@default",
    ) -> dict:
        """Update an existing task."""
        task = self.service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
        if title is not None:
            task["title"] = title
        if notes is not None:
            task["notes"] = notes
        if due is not None:
            task["due"] = f"{due}T00:00:00.000Z" if due else ""
        result = self.service.tasks().update(tasklist=tasklist_id, task=task_id, body=task).execute()
        return {"id": result["id"], "title": result["title"], "status": result.get("status", "")}

    def delete_task(self, task_id: str, tasklist_id: str = "@default") -> str:
        """Delete a task."""
        self.service.tasks().delete(tasklist=tasklist_id, task=task_id).execute()
        return "Task deleted."


TASKS_TOOLS = [
    {
        "name": "list_tasklists",
        "description": "List all Google Tasks lists.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_tasks",
        "description": "List tasks in a Google Tasks list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID (use '@default' for the default list)",
                    "default": "@default",
                },
                "show_completed": {
                    "type": "boolean",
                    "description": "Whether to include completed tasks",
                    "default": False,
                },
            },
            "required": [],
        },
    },
    {
        "name": "create_task",
        "description": "Create a new task in Google Tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "notes": {"type": "string", "description": "Task notes/description", "default": ""},
                "due": {"type": "string", "description": "Due date in YYYY-MM-DD format", "default": ""},
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID (use '@default' for the default list)",
                    "default": "@default",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task as completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID of the task to complete"},
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID",
                    "default": "@default",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "update_task",
        "description": "Update an existing task's title, notes, or due date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID of the task to update"},
                "title": {"type": "string", "description": "New title"},
                "notes": {"type": "string", "description": "New notes"},
                "due": {"type": "string", "description": "New due date (YYYY-MM-DD)"},
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID",
                    "default": "@default",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_task",
        "description": "Delete a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID of the task to delete"},
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID",
                    "default": "@default",
                },
            },
            "required": ["task_id"],
        },
    },
]


def execute_tool(client: GoogleTasksClient, tool_name: str, tool_input: dict) -> str:
    """Execute a Google Tasks tool call."""
    try:
        if tool_name == "list_tasklists":
            result = client.list_tasklists()
        elif tool_name == "list_tasks":
            result = client.list_tasks(
                tool_input.get("tasklist_id", "@default"),
                tool_input.get("show_completed", False),
            )
        elif tool_name == "create_task":
            result = client.create_task(
                tool_input["title"],
                tool_input.get("notes", ""),
                tool_input.get("due", ""),
                tool_input.get("tasklist_id", "@default"),
            )
        elif tool_name == "complete_task":
            result = client.complete_task(
                tool_input["task_id"],
                tool_input.get("tasklist_id", "@default"),
            )
        elif tool_name == "update_task":
            result = client.update_task(
                tool_input["task_id"],
                title=tool_input.get("title"),
                notes=tool_input.get("notes"),
                due=tool_input.get("due"),
                tasklist_id=tool_input.get("tasklist_id", "@default"),
            )
        elif tool_name == "delete_task":
            result = client.delete_task(
                tool_input["task_id"],
                tool_input.get("tasklist_id", "@default"),
            )
        else:
            return f"Unknown tool: {tool_name}"

        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Google Tasks error: {e}"
