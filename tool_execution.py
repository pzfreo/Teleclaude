"""Synchronous tool dispatch for the API bot.

`_execute_tool_call` routes a single Anthropic tool_use block to the right
handler (GitHub, web, tasks, calendar, etc.) and returns the textual result.
The Pulse and Monitor tool handlers and the MCP routing live elsewhere; this
module covers everything that runs in a thread executor.

All integration clients and helper names are looked up via `bot` (lazily, to
avoid a circular import).
"""

from __future__ import annotations

import logging

from persistence import audit_log, save_todos

logger = logging.getLogger(__name__)


def _truncate_result(text: str, max_len: int = 10000) -> str:
    """Truncate text and append a marker if it exceeds max_len."""
    if len(text) > max_len:
        return text[:max_len] + "\n... (truncated)"
    return text


def _execute_tool_call(block, repo, chat_id) -> str:
    """Dispatch a single tool call to the right handler."""
    import bot

    try:
        if block.name == "update_todo_list":
            todos = block.input.get("todos", [])
            bot.chat_todos[chat_id] = todos
            save_todos(chat_id, todos)
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return bot.format_todo_list(todos)
        if block.name == "schedule_check":
            return bot._handle_schedule_check(block.input, chat_id)
        if block.name == "manage_pulse":
            audit_log("tool_call", chat_id=chat_id, detail=f"manage_pulse:{block.input.get('action', '')}")
            return bot._handle_manage_pulse(block.input, chat_id)
        if block.name == "web_search" and bot.execute_web_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}: {block.input.get('query', '')[:100]}")
            return bot.execute_web_tool(bot.web_client, block.name, block.input)
        elif block.name in bot._tasks_tool_names and bot.execute_tasks_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return bot.execute_tasks_tool(bot.tasks_client, block.name, block.input)
        elif block.name in bot._calendar_tool_names and bot.execute_calendar_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return bot.execute_calendar_tool(bot.calendar_client, block.name, block.input)
        elif block.name in bot._email_tool_names and bot.execute_email_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}: to={block.input.get('to', '')}")
            return bot.execute_email_tool(bot.email_client, block.name, block.input)
        elif block.name in bot._contacts_tool_names and bot.execute_contacts_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return bot.execute_contacts_tool(bot.contacts_client, block.name, block.input)
        elif block.name in bot._train_tool_names and bot.execute_train_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}: {block.input.get('station', '')}")
            return bot.execute_train_tool(bot.train_client, block.name, block.input)
        elif block.name in bot._github_tool_names and bot.execute_github_tool:
            if not repo:
                return "No active repo. Ask the user to set one with /repo owner/name first."
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name} on {repo}")
            result = bot.execute_github_tool(bot.gh_client, repo, block.name, block.input)
            # Auto-track branch
            if block.name == "create_branch":
                bot.set_active_branch(chat_id, block.input.get("branch_name"))
            elif block.name in ("create_or_update_file", "upload_binary_file", "delete_file", "commit_multiple_files"):
                branch = block.input.get("branch")
                if branch:
                    bot.set_active_branch(chat_id, branch)
            return result
        return f"Tool '{block.name}' is not available."
    except Exception as e:
        logger.error("Tool '%s' crashed: %s", block.name, e, exc_info=True)
        audit_log("tool_error", chat_id=chat_id, detail=f"{block.name}: {e}")
        return f"Tool error: {e}"
