"""Teleclaude - Chat with Claude on Telegram. Code against GitHub."""

from pathlib import Path as _Path

VERSION = (_Path(__file__).parent / "VERSION").read_text().strip()

import asyncio
import base64
import collections
import datetime
import io
import json
import logging
import os
import re
import sys
import time
from typing import Any

import anthropic
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from persistence import (
    audit_log,
    clear_conversation,
    count_monitors,
    delete_monitor,
    delete_pulse_goal,
    delete_schedule,
    disable_monitor,
    init_db,
    load_active_branch,
    load_active_repo,
    load_all_monitors,
    load_all_pulse_configs,
    load_all_schedules,
    load_conversation,
    load_model,
    load_monitors,
    load_plan_mode,
    load_pulse_config,
    load_pulse_goals,
    load_schedules,
    load_todos,
    save_active_branch,
    save_active_repo,
    save_conversation,
    save_model,
    save_monitor,
    save_plan_mode,
    save_pulse_config,
    save_pulse_goal,
    save_schedule,
    save_todos,
    update_monitor_result,
    update_pulse_last_run,
)
from shared import (
    RingBufferHandler,
    download_telegram_file,
    send_long_message,
)
from shared import (
    is_authorized as _is_authorized,
)

load_dotenv()


_ring_handler = RingBufferHandler()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Attach ring buffer to root logger so it captures everything
logging.getLogger().addHandler(_ring_handler)

# Silence noisy/leaky loggers:
#   httpx logs every HTTP request with full URL (includes bot token!)
#   googleapiclient.discovery_cache warns about oauth2client version
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# ── Required config ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

AVAILABLE_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _check_required_config() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Bot cannot start.")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY is not set. Bot cannot start.")
        sys.exit(1)


# ── Optional integrations (each loads gracefully) ────────────────────

# GitHub
gh_client = None
GITHUB_TOOLS: list[dict[str, Any]] = []
execute_github_tool = None
try:
    from github_tools import GITHUB_TOOLS, GitHubClient
    from github_tools import execute_tool as _execute_github

    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        gh_client = GitHubClient(token)
        execute_github_tool = _execute_github
        logger.info("GitHub integration: enabled")
    else:
        GITHUB_TOOLS = []
        logger.info("GitHub integration: disabled (no GITHUB_TOKEN)")
except Exception as e:
    logger.warning("GitHub integration: failed to load (%s)", e)
    GITHUB_TOOLS = []

# Web search
web_client = None
WEB_TOOLS: list[dict[str, Any]] = []
execute_web_tool = None
try:
    from web_tools import WEB_TOOLS, WebSearchClient
    from web_tools import execute_tool as _execute_web

    web_client = WebSearchClient()
    execute_web_tool = _execute_web
    logger.info("Web search: enabled")
except Exception as e:
    logger.warning("Web search: failed to load (%s)", e)
    WEB_TOOLS = []

# Google Tasks
tasks_client = None
TASKS_TOOLS: list[dict[str, Any]] = []
execute_tasks_tool = None
try:
    from tasks_tools import TASKS_TOOLS, GoogleTasksClient
    from tasks_tools import execute_tool as _execute_tasks

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if client_id and client_secret and refresh_token:
        tasks_client = GoogleTasksClient(client_id, client_secret, refresh_token)
        execute_tasks_tool = _execute_tasks
        logger.info("Google Tasks: enabled")
    else:
        TASKS_TOOLS = []
        logger.info("Google Tasks: disabled (missing Google credentials)")
except Exception as e:
    logger.warning("Google Tasks: failed to load (%s)", e)
    TASKS_TOOLS = []

# Google Calendar
calendar_client = None
CALENDAR_TOOLS: list[dict[str, Any]] = []
execute_calendar_tool = None
try:
    from calendar_tools import CALENDAR_TOOLS, GoogleCalendarClient
    from calendar_tools import execute_tool as _execute_calendar

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if client_id and client_secret and refresh_token:
        calendar_client = GoogleCalendarClient(client_id, client_secret, refresh_token)
        execute_calendar_tool = _execute_calendar
        logger.info("Google Calendar: enabled")
    else:
        CALENDAR_TOOLS = []
        logger.info("Google Calendar: disabled (missing Google credentials)")
except Exception as e:
    logger.warning("Google Calendar: failed to load (%s)", e)
    CALENDAR_TOOLS = []

# Gmail (send only)
email_client = None
EMAIL_TOOLS: list[dict[str, Any]] = []
execute_email_tool = None
try:
    from email_tools import EMAIL_TOOLS, GmailSendClient
    from email_tools import execute_tool as _execute_email

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if client_id and client_secret and refresh_token:
        email_client = GmailSendClient(client_id, client_secret, refresh_token)
        execute_email_tool = _execute_email
        logger.info("Gmail (send only): enabled")
    else:
        EMAIL_TOOLS = []
        logger.info("Gmail: disabled (missing Google credentials)")
except Exception as e:
    logger.warning("Gmail: failed to load (%s)", e)
    EMAIL_TOOLS = []

# Google Contacts
contacts_client = None
CONTACTS_TOOLS: list[dict[str, Any]] = []
execute_contacts_tool = None
try:
    from contacts_tools import CONTACTS_TOOLS, GoogleContactsClient
    from contacts_tools import execute_tool as _execute_contacts

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if client_id and client_secret and refresh_token:
        contacts_client = GoogleContactsClient(client_id, client_secret, refresh_token)
        execute_contacts_tool = _execute_contacts
        logger.info("Google Contacts: enabled")
    else:
        CONTACTS_TOOLS = []
        logger.info("Google Contacts: disabled (missing Google credentials)")
except Exception as e:
    logger.warning("Google Contacts: failed to load (%s)", e)
    CONTACTS_TOOLS = []

# UK Train Times (Huxley2 — no API key needed)
train_client = None
TRAIN_TOOLS: list[dict[str, Any]] = []
execute_train_tool = None
try:
    from train_tools import TRAIN_TOOLS, TrainClient
    from train_tools import execute_tool as _execute_train

    train_client = TrainClient()
    execute_train_tool = _execute_train
    logger.info("UK Train Times: enabled")
except Exception as e:
    logger.warning("UK Train Times: failed to load (%s)", e)
    TRAIN_TOOLS = []

# Voice transcription (OpenAI Whisper)
openai_client = None
try:
    import openai as _openai_module

    _openai_api_key = os.getenv("OPENAI_API_KEY", "")
    if _openai_api_key:
        openai_client = _openai_module.OpenAI(api_key=_openai_api_key)
        logger.info("Voice transcription (Whisper): enabled")
    else:
        logger.info("Voice transcription: disabled (no OPENAI_API_KEY)")
except Exception as e:
    logger.warning("Voice transcription: failed to load (%s)", e)

# MCP (Model Context Protocol) servers
mcp_manager = None
MCP_TOOLS: list[dict[str, Any]] = []
_mcp_config: dict[str, Any] | None = None
try:
    from mcp_tools import MCPManager, load_mcp_config

    _mcp_config = load_mcp_config()
    if _mcp_config:
        mcp_manager = MCPManager()
        logger.info("MCP servers: configured (%d server(s) pending init)", len(_mcp_config))
    else:
        logger.info("MCP servers: disabled (no MCP_SERVERS env var)")
except Exception as e:
    logger.warning("MCP servers: failed to load (%s)", e)

# ── Bot config ───────────────────────────────────────────────────────

USER_TIMEZONE = os.getenv("TIMEZONE", "UTC")
DAILY_BRIEFING_TIME = os.getenv("DAILY_BRIEFING_TIME", "")  # e.g. "08:00"

SYSTEM_PROMPT = """You are Teleclaude, a personal AI assistant on Telegram. You help with coding, productivity, and daily tasks.

You have access to: GitHub (code, issues, PRs, CI), web search, Google Tasks, Google Calendar, Gmail (send only), Google Contacts, and UK train times.

Coding guidelines:
- Always read existing files before modifying them.
- Create a feature branch for changes — never commit directly to main.
- Write clear commit messages.
- When making multiple file changes, do them on the same branch, then open a single PR.

Communication guidelines:
- Keep Telegram responses concise — this is a phone screen, not a desktop.
- Use short paragraphs. Avoid walls of text.
- Use code blocks sparingly — only for short snippets.
- When listing items, prefer compact formats.
- If a response would be very long, summarize and offer to elaborate.
- Use UK units and conventions: °C for temperature, miles for distance, metres for short distances, 24h or 12h time, dd/mm/yyyy dates.

Tool usage:
- Use web search to look up documentation, error messages, or current information.
- Use Google Tasks to manage the user's tasks when they ask about todos, reminders, or task management.
- Use Google Calendar to check schedule, create events, or manage the user's calendar.
- For time-specific events, use the user's timezone unless they specify otherwise.
- You can send emails via Gmail but NEVER send an email without explicitly confirming the recipient, subject, and body with the user first.
- Use Google Contacts to search, view, or manage the user's contacts.
- Use UK train times to check departures, arrivals, search stations, or get service details for National Rail.
- You can configure the Pulse autonomous agent using manage_pulse. Pulse periodically reviews the user's context
  (calendar, tasks, goals) and proactively sends updates when something needs attention. Most checks are silent.
  When the user says things like "keep an eye on my PRs", "remind me about overdue tasks", or "watch for X",
  use manage_pulse to add a goal. Use it to enable/disable Pulse, set intervals, and configure quiet hours.
  The user can also use /pulse as a shortcut.
- You can set up background monitors using schedule_check to watch for changes and alert the user.
  Use this when they want to be notified about future events (train delays, new issues, PR merges, etc.).
  Choose sensible intervals (trains: 5-10m, GitHub: 15-30m) and expiry times (until journey, end of day).
  The user can view active monitors with /monitors and remove them with /monitors remove <id>.
- When you need the user to choose between options, use the ask_user tool to present inline buttons.
- When plan mode is on, always outline your plan first and wait for user approval before executing.
- You can upload binary files (images, etc.) to GitHub repos using the upload_binary_file tool.
- If a GitHub tool says "No active repo", tell the user to set one with /repo owner/name.

Attachments:
- Users can send photos, documents (images, PDFs, text files), stickers, locations, and contacts.
- You can see and analyze images. You can read PDFs and text files.
- If a user sends an image and wants it saved to a repo, use upload_binary_file with the base64 data from the image.
- Voice messages and audio files are automatically transcribed if Whisper is configured.
- Video is not yet supported — ask the user to type instead.

You are a knowledgeable assistant across many domains — not just coding. You can help with writing, research, brainstorming, analysis, math, and general questions.

For coding tasks that need filesystem access (reading/writing files, running tests, git operations), tell the user to use the Agent bot instead — this bot handles API-based tasks like GitHub PRs/issues, web search, calendar, tasks, and email."""

# ── Internal tools (always available) ─────────────────────────────────

TODO_TOOL = {
    "name": "update_todo_list",
    "description": (
        "Update the task/todo list. ONLY use this when the user explicitly asks to track tasks, "
        "or during plan mode to track plan steps. Do NOT proactively create or maintain todos — "
        "most conversations don't need task tracking. Send the full updated list each time. "
        "Remove completed items instead of keeping them around."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "What needs to be done"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
}

ASK_USER_TOOL = {
    "name": "ask_user",
    "description": (
        "Ask the user to choose from a set of options via inline buttons. "
        "Use this when you need the user to make a choice before proceeding. "
        "Returns the user's selection as text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask the user"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 5,
                "description": "2-5 options for the user to choose from",
            },
        },
        "required": ["question", "options"],
    },
}

SCHEDULE_CHECK_TOOL = {
    "name": "schedule_check",
    "description": (
        "Schedule a recurring background check that monitors something and notifies the user "
        "when conditions change. Use this when the user wants to be alerted about future changes "
        "(e.g. train delays, new GitHub issues, PR merges). The check runs every interval_minutes "
        "until expires_at, using all available tools to gather current state. It compares each "
        "result with the previous one and only notifies the user if the notify_condition is met. "
        "First run captures a baseline silently. Auto-expires — max 24 hours. Max 5 active monitors."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "check_prompt": {
                "type": "string",
                "description": (
                    "The prompt to run each check cycle. Should instruct use of specific tools "
                    "(e.g. get_train_departures, list_issues). Be specific about what data to gather."
                ),
            },
            "notify_condition": {
                "type": "string",
                "description": (
                    "When to notify the user. Describe the change that matters. "
                    "E.g. 'Any train is delayed by more than 5 minutes or cancelled', "
                    "'A new issue was opened', 'The CI check failed'"
                ),
            },
            "interval_minutes": {
                "type": "integer",
                "description": "How often to check, in minutes. Min 5, max 60. Use 5-10 for trains, 15-30 for GitHub.",
                "minimum": 5,
                "maximum": 60,
            },
            "expires_at": {
                "type": "string",
                "description": (
                    "ISO 8601 datetime when monitoring should stop. Max 24h from now. "
                    "Choose sensibly: train monitoring until journey time, GitHub until end of day."
                ),
            },
            "summary": {
                "type": "string",
                "description": "Brief human-readable label, e.g. 'CHI→VIC train delays', 'New issues on Teleclaude'",
            },
        },
        "required": ["check_prompt", "notify_condition", "interval_minutes", "expires_at", "summary"],
    },
}

MANAGE_PULSE_TOOL = {
    "name": "manage_pulse",
    "description": (
        "Configure the autonomous Pulse agent. Pulse periodically reviews your context (calendar, tasks, goals) "
        "and proactively sends helpful updates when something needs attention. Most pulses are silent — "
        "it only messages you when there's something worth knowing. "
        "Use this when the user wants to: add/remove goals for Pulse to watch, enable/disable Pulse, "
        "change check interval, set quiet hours, or check Pulse status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "add_goal",
                    "remove_goal",
                    "list_goals",
                    "enable",
                    "disable",
                    "set_interval",
                    "set_quiet_hours",
                    "status",
                ],
                "description": "The action to perform.",
            },
            "goal": {
                "type": "string",
                "description": "For add_goal: description of what Pulse should watch for.",
            },
            "priority": {
                "type": "string",
                "enum": ["high", "normal", "low"],
                "description": "For add_goal: priority level. Default normal.",
            },
            "goal_id": {
                "type": "integer",
                "description": "For remove_goal: the goal ID to remove.",
            },
            "interval_minutes": {
                "type": "integer",
                "description": "For set_interval: how often to check, in minutes (15-240).",
                "minimum": 15,
                "maximum": 240,
            },
            "quiet_start": {
                "type": "string",
                "description": "For set_quiet_hours: start of quiet period, e.g. '22:00'.",
            },
            "quiet_end": {
                "type": "string",
                "description": "For set_quiet_hours: end of quiet period, e.g. '07:00'.",
            },
        },
        "required": ["action"],
    },
}

# Registered pulse jobs: chat_id -> Job
_pulse_jobs: dict[int, Any] = {}
# In-memory pulse config cache: chat_id -> dict
_pulse_configs: dict[int, dict] = {}

MAX_MONITORS_PER_CHAT = 5
MAX_MONITOR_DURATION_HOURS = 24
# Registered monitor jobs: monitor_id -> Job
_monitor_jobs: dict[int, Any] = {}

ASK_USER_TIMEOUT = 300  # seconds to wait for user response
_ask_user_futures: dict[int, asyncio.Future] = {}

ALLOWED_USER_IDS: set[int] = set()
for uid in os.getenv("ALLOWED_USER_IDS", "").split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USER_IDS.add(int(uid))

MAX_HISTORY = 50
MAX_TELEGRAM_LENGTH = 4096
MAX_TOOL_ROUNDS = 15
TYPING_INTERVAL = 4  # seconds between typing indicator refreshes
PROGRESS_INTERVAL = 15  # seconds before sending a progress message

api_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
async_api_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Build tool name sets for dispatch
_github_tool_names = {t["name"] for t in GITHUB_TOOLS}
_tasks_tool_names = {t["name"] for t in TASKS_TOOLS}
_calendar_tool_names = {t["name"] for t in CALENDAR_TOOLS}
_email_tool_names = {t["name"] for t in EMAIL_TOOLS}
_contacts_tool_names = {t["name"] for t in CONTACTS_TOOLS}
_train_tool_names = {t["name"] for t in TRAIN_TOOLS}

# In-memory cache (backed by SQLite)
conversations: dict[int, list] = {}
active_repos: dict[int, str] = {}
active_branches: dict[int, str] = {}
chat_models: dict[int, str] = {}
chat_todos: dict[int, list[dict]] = {}
chat_plan_mode: dict[int, bool] = {}
# Per-chat locks to prevent concurrent message handling corruption
_chat_locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
# Per-chat cancel events for ! interrupt
_cancel_events: dict[int, asyncio.Event] = {}
# Registered schedule jobs: schedule_id -> Job
_scheduled_jobs: dict[int, Any] = {}


async def _call_anthropic(**kwargs) -> anthropic.types.Message:
    """Call Anthropic API with retry on transient errors (rate limit, overloaded).

    Runs the synchronous SDK call in a thread to avoid blocking the event loop.
    """
    loop = asyncio.get_running_loop()
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            return await loop.run_in_executor(None, lambda: api_client.messages.create(**kwargs))
        except anthropic.RateLimitError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                logger.warning("Rate limited, retrying in %ds (attempt %d/%d)", wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
            else:
                raise
        except anthropic.InternalServerError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                logger.warning("API overloaded, retrying in %ds (attempt %d/%d)", wait, attempt + 1, max_retries)
                await asyncio.sleep(wait)
            else:
                raise
    raise RuntimeError("Unreachable: retry loop completed without returning or raising")


async def _stream_round(
    kwargs: dict,
    chat_id: int,
    bot,
    stop_typing: asyncio.Event,
    cancel: asyncio.Event | None = None,
) -> tuple[anthropic.types.Message, str | None]:
    """Execute one API round with streaming.

    Returns (final_message, streamed_text_or_none).
    If text was streamed to Telegram, streamed_text is the full text.
    If no text was produced, streamed_text is None.

    Raises anthropic.RateLimitError or anthropic.InternalServerError
    on transient failures for the caller to catch and fall back.
    """
    from streaming import StreamingResponder

    responder = StreamingResponder(bot, chat_id)
    first_text = True

    async with async_api_client.messages.stream(**kwargs) as stream:
        async for event in stream:
            if cancel and cancel.is_set():
                raise asyncio.CancelledError("User cancelled request")
            if (
                event.type == "content_block_delta"
                and hasattr(event.delta, "type")
                and event.delta.type == "text_delta"
            ):
                if first_text:
                    stop_typing.set()
                    first_text = False
                await responder.feed(event.delta.text)

        final_message = await stream.get_final_message()

    await responder.finalize()

    streamed_text = responder.full_text if responder.full_text else None
    return final_message, streamed_text


def get_model(chat_id: int) -> str:
    if chat_id not in chat_models:
        saved = load_model(chat_id)
        if saved:
            chat_models[chat_id] = saved
    return chat_models.get(chat_id, DEFAULT_MODEL)


def get_todos(chat_id: int) -> list[dict]:
    if chat_id not in chat_todos:
        chat_todos[chat_id] = load_todos(chat_id)
    return chat_todos[chat_id]


def get_plan_mode(chat_id: int) -> bool:
    if chat_id not in chat_plan_mode:
        chat_plan_mode[chat_id] = load_plan_mode(chat_id)
    return chat_plan_mode[chat_id]


def format_todo_list(todos: list[dict]) -> str:
    if not todos:
        return "No tasks tracked."
    icons = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = []
    for i, t in enumerate(todos, 1):
        icon = icons.get(t.get("status", "pending"), "[ ]")
        lines.append(f"{icon} {i}. {t['content']}")
    return "\n".join(lines)


def is_authorized(user_id: int) -> bool:
    return _is_authorized(user_id, ALLOWED_USER_IDS)


def get_conversation(chat_id: int) -> list:
    """Get conversation from cache or load from DB (sanitized)."""
    if chat_id not in conversations:
        loaded = load_conversation(chat_id)
        conversations[chat_id] = _sanitize_history(loaded)
    return conversations[chat_id]


def get_active_repo(chat_id: int) -> str | None:
    """Get active repo from cache or load from DB."""
    if chat_id not in active_repos:
        repo = load_active_repo(chat_id)
        if repo:
            active_repos[chat_id] = repo
    return active_repos.get(chat_id)


def get_active_branch(chat_id: int) -> str | None:
    """Get active branch from cache or load from DB."""
    if chat_id not in active_branches:
        branch = load_active_branch(chat_id)
        if branch:
            active_branches[chat_id] = branch
    return active_branches.get(chat_id)


def set_active_branch(chat_id: int, branch: str | None) -> None:
    if branch:
        active_branches[chat_id] = branch
    elif chat_id in active_branches:
        del active_branches[chat_id]
    save_active_branch(chat_id, branch)


MAX_CONTENT_SIZE = 20000  # max chars per content string in history


def _trim_content(content, keep_images: bool = True) -> Any:
    """Truncate oversized content blocks when reloading history.

    When keep_images is False, replace image/document blocks with text placeholders
    to save context space for older messages.
    """
    if isinstance(content, str) and len(content) > MAX_CONTENT_SIZE:
        return content[:MAX_CONTENT_SIZE] + "\n... (truncated)"
    if isinstance(content, list):
        trimmed = []
        for item in content:
            if isinstance(item, dict):
                item = dict(item)  # shallow copy
                # Strip binary data from old messages
                if not keep_images and item.get("type") == "image":
                    trimmed.append({"type": "text", "text": "[image was here]"})
                    continue
                if not keep_images and item.get("type") == "document":
                    trimmed.append({"type": "text", "text": "[document was here]"})
                    continue
                if isinstance(item.get("content"), str) and len(item["content"]) > MAX_CONTENT_SIZE:
                    item["content"] = item["content"][:MAX_CONTENT_SIZE] + "\n... (truncated)"
            trimmed.append(item)
        return trimmed
    return content


# Number of recent messages to keep images for (the rest get stripped)
_KEEP_IMAGES_LAST_N = 10


def _sanitize_history(history: list[dict]) -> list[dict]:
    """Ensure history is valid for the Anthropic API.

    - Every tool_use block must have a matching tool_result in the next message.
    - History must start with a user message.
    - Remove thinking blocks (they cause issues when sent back).
    """
    if not history:
        return history

    # Clean assistant content blocks:
    # - Convert SDK objects to plain dicts (SDK objects bypass isinstance(b, dict) checks below)
    # - Strip thinking blocks (they can't be replayed)
    # - Remove SDK-internal fields like parsed_output that the API rejects
    _KNOWN_TEXT_KEYS = {"type", "text"}
    _KNOWN_TOOL_USE_KEYS = {"type", "id", "name", "input"}
    for msg in history:
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            cleaned = []
            for b in msg["content"]:
                # Convert SDK objects (e.g. ToolUseBlock, TextBlock) to plain dicts
                if not isinstance(b, dict) and hasattr(b, "model_dump"):
                    b = b.model_dump(exclude_none=True)
                elif not isinstance(b, dict) and hasattr(b, "__dict__"):
                    b = dict(b.__dict__)
                if not isinstance(b, dict):
                    continue  # Skip non-dict, non-SDK items we can't process
                if b.get("type") == "thinking":
                    continue
                if b.get("type") == "text":
                    cleaned.append({k: v for k, v in b.items() if k in _KNOWN_TEXT_KEYS})
                elif b.get("type") == "tool_use":
                    cleaned.append({k: v for k, v in b.items() if k in _KNOWN_TOOL_USE_KEYS})
                else:
                    cleaned.append(b)
            msg["content"] = cleaned
        # Also convert SDK objects in user messages (tool_result blocks)
        elif msg.get("role") == "user" and isinstance(msg.get("content"), list):
            cleaned = []
            for b in msg["content"]:
                if not isinstance(b, dict) and hasattr(b, "model_dump"):
                    b = b.model_dump(exclude_none=True)
                elif not isinstance(b, dict) and hasattr(b, "__dict__"):
                    b = dict(b.__dict__)
                if isinstance(b, dict):
                    cleaned.append(b)
            if cleaned:
                msg["content"] = cleaned

    # Walk forward and remove orphaned tool_use/tool_result pairs
    sanitized = []
    i = 0
    while i < len(history):
        msg = history[i]

        # Check if this assistant message has tool_use blocks
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            tool_use_ids = {
                b["id"] for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b
            }

            if tool_use_ids:
                # There must be a next message with matching tool_results
                if i + 1 < len(history):
                    next_msg = history[i + 1]
                    if next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
                        result_ids = {
                            b.get("tool_use_id")
                            for b in next_msg["content"]
                            if isinstance(b, dict) and b.get("type") == "tool_result"
                        }
                        if tool_use_ids == result_ids:
                            # Pair is complete, keep both
                            sanitized.append(msg)
                            sanitized.append(next_msg)
                            i += 2
                            continue
                # Pair is broken — skip the assistant message
                logger.warning("Dropping orphaned tool_use message at index %d", i)
                # Also skip the next message if it contains orphaned tool_results
                if i + 1 < len(history):
                    next_msg = history[i + 1]
                    if next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
                        has_tool_results = any(
                            isinstance(b, dict) and b.get("type") == "tool_result" for b in next_msg["content"]
                        )
                        if has_tool_results:
                            # Strip tool_result blocks, keep any other content (e.g. text)
                            kept = [
                                b
                                for b in next_msg["content"]
                                if not (isinstance(b, dict) and b.get("type") == "tool_result")
                            ]
                            if kept:
                                sanitized.append({"role": "user", "content": kept})
                            logger.warning("Stripped orphaned tool_result blocks from message at index %d", i + 1)
                            i += 2
                            continue
                i += 1
                continue

        # Check if a user message has tool_result blocks without a preceding tool_use
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            has_tool_results = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"])
            if has_tool_results:
                # Check if previous sanitized message is an assistant with matching tool_use
                prev = sanitized[-1] if sanitized else None
                prev_tool_ids: set[str] = set()
                if prev and prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                    prev_tool_ids = {
                        b["id"]
                        for b in prev["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b
                    }
                result_ids = {
                    b.get("tool_use_id")
                    for b in msg["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                if not prev_tool_ids or not (result_ids <= prev_tool_ids):
                    # Orphaned tool_results — strip them, keep other content
                    kept = [b for b in msg["content"] if not (isinstance(b, dict) and b.get("type") == "tool_result")]
                    if kept:
                        sanitized.append({"role": "user", "content": kept})
                    logger.warning("Stripped orphaned tool_result blocks from user message at index %d", i)
                    i += 1
                    continue

        sanitized.append(msg)
        i += 1

    # Ensure history starts with a user message
    while sanitized and sanitized[0].get("role") != "user":
        sanitized.pop(0)

    # Removing orphans can create new orphans (e.g. a tool_result whose tool_use
    # was inside a dropped pair). Re-run until stable.
    if len(sanitized) < len(history):
        return _sanitize_history(sanitized)

    return sanitized


def trim_history(chat_id: int) -> None:
    history = get_conversation(chat_id)
    if len(history) > MAX_HISTORY * 2:
        del history[: len(history) - MAX_HISTORY * 2]
    # Sanitize to fix any broken tool_use/tool_result pairs
    # IMPORTANT: modify in-place to preserve list reference held by _process_message
    sanitized = _sanitize_history(history)
    history.clear()
    history.extend(sanitized)
    cutoff = max(0, len(history) - _KEEP_IMAGES_LAST_N)
    for i, msg in enumerate(history):
        msg["content"] = _trim_content(msg.get("content"), keep_images=(i >= cutoff))


def save_state(chat_id: int) -> None:
    """Persist current conversation to SQLite."""
    save_conversation(chat_id, get_conversation(chat_id))


_EXTENDED_THINKING_PATTERN = re.compile(
    r"\b(?:think\s+(?:about|through|deeply|step\s+by\s+step)|"
    r"reason\s+(?:through|about|carefully)|"
    r"analyze\s+carefully)\b",
    re.IGNORECASE,
)


def _wants_extended_thinking(user_content) -> bool:
    """Check if the user's message contains keywords requesting deeper reasoning."""
    if isinstance(user_content, str):
        return bool(_EXTENDED_THINKING_PATTERN.search(user_content))
    if isinstance(user_content, list):
        for block in user_content:
            if isinstance(block, dict) and block.get("type") == "text":
                if _EXTENDED_THINKING_PATTERN.search(block.get("text", "")):
                    return True
    return False


async def keep_typing(chat, stop_event: asyncio.Event, start_time: float, bot, status: dict):
    """Keep the typing indicator alive and send progress updates.

    status dict is shared with the tool loop:
      - status["round"]: current tool round number
      - status["max"]: max tool rounds
      - status["tools"]: list of tool names called so far
      - status["last_update_round"]: last round we sent a progress message for
    """
    last_update_round = -1
    while not stop_event.is_set():
        try:
            await chat.send_action("typing")
        except TelegramError:
            pass  # chat may have been deleted or bot blocked
        elapsed = time.time() - start_time
        current_round = status.get("round", 0)
        # Send a progress update when: enough time has passed AND there's new tool activity
        if elapsed > PROGRESS_INTERVAL and current_round > last_update_round:
            tools_used = status.get("tools", [])
            max_rounds = status.get("max", 15)
            if tools_used:
                recent = tools_used[-3:]  # last 3 tools
                tool_summary = ", ".join(recent)
                msg = f"[{current_round}/{max_rounds}] {tool_summary}"
            else:
                msg = "Thinking..."
            try:
                await bot.send_message(chat_id=chat.id, text=msg)
            except TelegramError:
                pass
            last_update_round = current_round
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TYPING_INTERVAL)
            break
        except TimeoutError:
            continue


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    status_lines = []
    status_lines.append(f"GitHub: {'enabled' if gh_client else 'not configured'}")
    status_lines.append(f"Web search: {'enabled' if web_client else 'not configured'}")
    status_lines.append(f"Google Tasks: {'enabled' if tasks_client else 'not configured'}")
    status_lines.append(f"Google Calendar: {'enabled' if calendar_client else 'not configured'}")
    status_lines.append(f"Gmail (send): {'enabled' if email_client else 'not configured'}")
    status = "\n".join(status_lines)

    await update.message.reply_text(
        "Hello! I'm Teleclaude — Claude on Telegram.\n\n"
        "Commands:\n"
        "/repo owner/name - Set the active GitHub repo\n"
        "/repo - Show current repo and branch\n"
        "/branch name - Set active branch (/branch clear to reset)\n"
        "/new - Start a fresh conversation\n"
        "/model - Show or switch Claude model (opus/sonnet/haiku)\n"
        "/plan - Toggle plan mode (outline before executing)\n"
        "/todo - Show current task list (/todo clear to reset)\n"
        "/pulse - Autonomous agent status and config\n"
        "/schedule - Manage recurring scheduled prompts\n"
        "/briefing - Get a daily summary of calendar + tasks\n"
        "/logs [min] - Download recent logs (default 5 min)\n"
        "/version - Show bot version\n"
        "/help - Show this message\n\n"
        f"{status}"
    )


async def set_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        repo = get_active_repo(chat_id)
        lines = []
        if repo:
            branch = get_active_branch(chat_id)
            lines.append(f"Active repo: {repo}" + (f" ({branch})" if branch else ""))
            lines.append("")

        # List recent repos if GitHub is configured
        if gh_client:
            try:
                loop = asyncio.get_running_loop()
                repos = await loop.run_in_executor(None, gh_client.list_user_repos, 5)
                lines.append("Recent repos:")
                for i, r in enumerate(repos, 1):
                    desc = f" — {r['description']}" if r["description"] else ""
                    marker = " *" if r["full_name"] == repo else ""
                    lines.append(f"  {i}. {r['full_name']}{desc}{marker}")
                lines.append("\n/repo <number> or /repo owner/name")
            except Exception as e:
                logger.warning("Failed to list repos: %s", e)
                if not repo:
                    lines.append("No repo set. Use: /repo owner/name")
        elif not repo:
            lines.append("No repo set. Use: /repo owner/name")

        await update.message.reply_text("\n".join(lines))
        return

    arg = context.args[0]

    # Check if it's a number (pick from recent list)
    if arg.isdigit() and gh_client:
        try:
            loop = asyncio.get_running_loop()
            repos = await loop.run_in_executor(None, gh_client.list_user_repos, 5)
            idx = int(arg) - 1
            if 0 <= idx < len(repos):
                repo = repos[idx]["full_name"]
                # Fall through to set this repo
            else:
                await update.message.reply_text(f"Invalid number. Use 1-{len(repos)}.")
                return
        except Exception as e:
            await update.message.reply_text(f"Failed to list repos: {e}")
            return
    else:
        repo = arg
        if "/" not in repo or len(repo.split("/")) != 2:
            await update.message.reply_text("Format: /repo owner/name (e.g. /repo pzfreo/Teleclaude)")
            return

    if not gh_client:
        await update.message.reply_text("GitHub not configured. Set GITHUB_TOKEN in environment.")
        return

    try:
        default_branch = gh_client.get_default_branch(repo)
        active_repos[chat_id] = repo
        save_active_repo(chat_id, repo)
        set_active_branch(chat_id, None)  # reset branch on repo switch
        await update.message.reply_text(f"Active repo set to: {repo} (default branch: {default_branch})")
    except Exception as e:
        await update.message.reply_text(f"Can't access {repo}: {e}")


async def new_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    chat_todos[chat_id] = []
    chat_plan_mode[chat_id] = False
    clear_conversation(chat_id)
    save_todos(chat_id, [])
    save_plan_mode(chat_id, False)
    set_active_branch(chat_id, None)
    await update.message.reply_text("Conversation cleared. Starting fresh.")


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        model = get_model(chat_id)
        shortcuts = ", ".join(AVAILABLE_MODELS.keys())
        await update.message.reply_text(
            f"Current model: {model}\n\n"
            f"Switch with: /model <name>\n"
            f"Shortcuts: {shortcuts}\n"
            f"Or use a full model ID, e.g. /model claude-sonnet-4-20250514"
        )
        return

    choice = context.args[0].lower().strip()

    if choice in AVAILABLE_MODELS:
        model_id = AVAILABLE_MODELS[choice]
    elif choice.startswith("claude-"):
        model_id = choice
    else:
        shortcuts = "\n".join(f"  {k} → {v}" for k, v in AVAILABLE_MODELS.items())
        await update.message.reply_text(
            f"Unknown model: {choice}\n\nAvailable shortcuts:\n{shortcuts}\n\n"
            f"Or use a full model ID starting with claude-"
        )
        return

    chat_models[chat_id] = model_id
    save_model(chat_id, model_id)
    await update.message.reply_text(f"Model switched to: {model_id}")


async def toggle_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    current = get_plan_mode(chat_id)
    new_mode = not current
    chat_plan_mode[chat_id] = new_mode
    save_plan_mode(chat_id, new_mode)
    if new_mode:
        await update.message.reply_text(
            "Plan mode ON. I'll outline a plan before making changes and wait for your approval."
        )
    else:
        await update.message.reply_text("Plan mode OFF. I'll execute tasks directly.")


async def show_todos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    todos = get_todos(chat_id)
    if context.args and context.args[0].lower() == "clear":
        chat_todos[chat_id] = []
        save_todos(chat_id, [])
        await update.message.reply_text("Todo list cleared.")
        return
    await update.message.reply_text(format_todo_list(todos))


async def send_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/logs [minutes] — send recent logs as a text file attachment."""
    if not is_authorized(update.effective_user.id):
        return
    minutes = 5
    if context.args:
        try:
            minutes = max(1, min(int(context.args[0]), 60))
        except ValueError:
            pass
    lines = _ring_handler.get_recent(seconds=minutes * 60)
    if not lines:
        await update.message.reply_text(f"No logs in the last {minutes} minute(s).")
        return
    content = "\n".join(lines)
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"teleclaude_logs_{minutes}min.txt"
    await update.message.reply_document(document=buf, caption=f"Last {minutes} min — {len(lines)} lines")


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(f"Teleclaude v{VERSION}\nModel: {get_model(update.effective_chat.id)}")


async def set_branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    repo = get_active_repo(chat_id)

    if not context.args:
        # List branches from the repo
        if not repo:
            await update.message.reply_text("No repo set. Use /repo first.")
            return
        if not gh_client:
            await update.message.reply_text("GitHub not configured.")
            return
        try:
            loop = asyncio.get_running_loop()
            branches = await loop.run_in_executor(None, gh_client.list_branches, repo)
            current = get_active_branch(chat_id)
            lines = []
            for i, b in enumerate(branches, 1):
                marker = " *" if b == current else ""
                lines.append(f"  {i}. {b}{marker}")
            header = f"Branches on {repo}:"
            if current:
                header += f"\n(active: {current})"
            await update.message.reply_text(f"{header}\n" + "\n".join(lines) + "\n\n/branch <number> or /branch <name>")
        except Exception as e:
            await update.message.reply_text(f"Failed to list branches: {e}")
        return

    arg = context.args[0].lower()

    if arg == "clear":
        set_active_branch(chat_id, None)
        await update.message.reply_text("Branch cleared. Claude will use the default branch.")
        return

    # Check if it's a number (pick from list)
    if arg.isdigit() and repo and gh_client:
        try:
            loop = asyncio.get_running_loop()
            branches = await loop.run_in_executor(None, gh_client.list_branches, repo)
            idx = int(arg) - 1
            if 0 <= idx < len(branches):
                branch = branches[idx]
                set_active_branch(chat_id, branch)
                await update.message.reply_text(f"Active branch set to: {branch}")
                return
            else:
                await update.message.reply_text(f"Invalid number. Use 1-{len(branches)}.")
                return
        except Exception as e:
            await update.message.reply_text(f"Failed to list branches: {e}")
            return

    # Literal branch name
    branch_name = context.args[0]
    set_active_branch(chat_id, branch_name)
    await update.message.reply_text(f"Active branch set to: {branch_name}")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_authorized(update.effective_user.id):
        return
    cmd = update.message.text.split()[0] if update.message.text else "/?"
    await update.message.reply_text(f"Unknown command: {cmd}\nType /help to see available commands.")


def _execute_tool_call(block, repo, chat_id) -> str:
    """Dispatch a single tool call to the right handler."""
    try:
        if block.name == "update_todo_list":
            todos = block.input.get("todos", [])
            chat_todos[chat_id] = todos
            save_todos(chat_id, todos)
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return format_todo_list(todos)
        if block.name == "schedule_check":
            return _handle_schedule_check(block.input, chat_id)
        if block.name == "manage_pulse":
            audit_log("tool_call", chat_id=chat_id, detail=f"manage_pulse:{block.input.get('action', '')}")
            return _handle_manage_pulse(block.input, chat_id)
        if block.name == "web_search" and execute_web_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}: {block.input.get('query', '')[:100]}")
            return execute_web_tool(web_client, block.name, block.input)
        elif block.name in _tasks_tool_names and execute_tasks_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return execute_tasks_tool(tasks_client, block.name, block.input)
        elif block.name in _calendar_tool_names and execute_calendar_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return execute_calendar_tool(calendar_client, block.name, block.input)
        elif block.name in _email_tool_names and execute_email_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}: to={block.input.get('to', '')}")
            return execute_email_tool(email_client, block.name, block.input)
        elif block.name in _contacts_tool_names and execute_contacts_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}")
            return execute_contacts_tool(contacts_client, block.name, block.input)
        elif block.name in _train_tool_names and execute_train_tool:
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name}: {block.input.get('station', '')}")
            return execute_train_tool(train_client, block.name, block.input)
        elif block.name in _github_tool_names and execute_github_tool:
            if not repo:
                return "No active repo. Ask the user to set one with /repo owner/name first."
            audit_log("tool_call", chat_id=chat_id, detail=f"{block.name} on {repo}")
            result = execute_github_tool(gh_client, repo, block.name, block.input)
            # Auto-track branch
            if block.name == "create_branch":
                set_active_branch(chat_id, block.input.get("branch_name"))
            elif block.name in ("create_or_update_file", "upload_binary_file", "delete_file", "commit_multiple_files"):
                branch = block.input.get("branch")
                if branch:
                    set_active_branch(chat_id, branch)
            return result
        return f"Tool '{block.name}' is not available."
    except Exception as e:
        logger.error("Tool '%s' crashed: %s", block.name, e, exc_info=True)
        audit_log("tool_error", chat_id=chat_id, detail=f"{block.name}: {e}")
        return f"Tool error: {e}"


def _handle_schedule_check(tool_input: dict, chat_id: int) -> str:
    """Handle the schedule_check tool call — validate and persist a new monitor."""
    # Validate limits
    active_count = count_monitors(chat_id)
    if active_count >= MAX_MONITORS_PER_CHAT:
        return f"Monitor limit reached ({MAX_MONITORS_PER_CHAT} active). Remove one with /monitors remove <id> first."

    check_prompt = tool_input.get("check_prompt", "")
    notify_condition = tool_input.get("notify_condition", "")
    interval_minutes = tool_input.get("interval_minutes", 10)
    expires_at_str = tool_input.get("expires_at", "")
    summary = tool_input.get("summary", "Monitor")

    if not check_prompt or not notify_condition:
        return "Error: check_prompt and notify_condition are required."

    interval_minutes = max(5, min(60, int(interval_minutes)))

    # Parse expiry
    try:
        import zoneinfo

        tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.UTC

    now = datetime.datetime.now(tz)
    try:
        expires_dt = datetime.datetime.fromisoformat(expires_at_str)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=tz)
    except (ValueError, TypeError):
        # Default: 2 hours from now
        expires_dt = now + datetime.timedelta(hours=2)

    # Cap at 24 hours
    max_expiry = now + datetime.timedelta(hours=MAX_MONITOR_DURATION_HOURS)
    if expires_dt > max_expiry:
        expires_dt = max_expiry
    if expires_dt <= now:
        return "Error: expires_at must be in the future."

    expires_at_ts = expires_dt.timestamp()

    monitor_id = save_monitor(
        chat_id=chat_id,
        check_prompt=check_prompt,
        notify_condition=notify_condition,
        interval_minutes=interval_minutes,
        expires_at=expires_at_ts,
        summary=summary,
    )

    # Register with job queue (needs to happen on the event loop)
    # Store pending registration — picked up by the async tool loop in _process_message
    _pending_monitor_registrations.append(
        {
            "id": monitor_id,
            "chat_id": chat_id,
            "check_prompt": check_prompt,
            "notify_condition": notify_condition,
            "interval_minutes": interval_minutes,
            "expires_at": expires_at_ts,
            "summary": summary,
            "last_result": None,
        }
    )

    expires_str = (
        expires_dt.strftime("%H:%M %Z") if expires_dt.date() == now.date() else expires_dt.strftime("%b %d %H:%M")
    )
    audit_log("monitor_created", chat_id=chat_id, detail=f"#{monitor_id}: {summary}")
    return (
        f"Monitor #{monitor_id} created: {summary}\n"
        f"Checking every {interval_minutes}m until {expires_str}.\n"
        f"First check will run shortly to capture a baseline."
    )


# Pending registrations from sync _execute_tool_call → picked up by async _process_message
_pending_monitor_registrations: list[dict] = []


async def _handle_ask_user(block, chat_id: int, bot) -> str:
    """Send inline keyboard to user and wait for their selection."""
    question = block.input.get("question", "Please choose:")
    options = block.input.get("options", [])

    if len(options) < 2:
        return "Error: ask_user requires at least 2 options."
    if len(options) > 5:
        options = options[:5]

    keyboard = []
    for i, option in enumerate(options):
        keyboard.append([InlineKeyboardButton(option, callback_data=f"ask_user:{chat_id}:{i}")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await bot.send_message(chat_id=chat_id, text=question, reply_markup=reply_markup)
    except TelegramError as e:
        return f"Failed to send question: {e}"

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    _ask_user_futures[chat_id] = future

    try:
        result = await asyncio.wait_for(future, timeout=ASK_USER_TIMEOUT)
        return f"User selected: {result}"
    except TimeoutError:
        return "User did not respond within the timeout period."
    finally:
        _ask_user_futures.pop(chat_id, None)


async def _ask_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for ask_user tool."""
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "ask_user":
        return

    try:
        chat_id = int(parts[1])
        option_index = int(parts[2])
    except (ValueError, IndexError):
        return

    await query.answer()

    # Get the selected text from the button
    selected_text = query.data  # fallback
    if query.message and query.message.reply_markup:
        rows = query.message.reply_markup.inline_keyboard
        if 0 <= option_index < len(rows) and rows[option_index]:
            selected_text = rows[option_index][0].text

    # Edit the message to show the selection (remove buttons)
    try:
        original_text = query.message.text or "Question"
        await query.edit_message_text(f"{original_text}\n\nSelected: {selected_text}")
    except TelegramError:
        pass

    # Resolve the future
    future = _ask_user_futures.get(chat_id)
    if future and not future.done():
        future.set_result(selected_text)


_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_SUPPORTED_DOC_MIMES = _IMAGE_MIME_TYPES | {"application/pdf"}


async def _download_telegram_file(file_obj, bot) -> bytes:
    """Download a Telegram file and return its bytes."""
    return await download_telegram_file(file_obj, bot)


async def _transcribe_voice(file_obj, bot, filename: str = "voice.ogg") -> str:
    """Transcribe a voice/audio file using OpenAI Whisper API."""
    data = await _download_telegram_file(file_obj, bot)
    buf = io.BytesIO(data)
    buf.name = filename
    loop = asyncio.get_running_loop()
    transcript = await loop.run_in_executor(
        None,
        lambda: openai_client.audio.transcriptions.create(model="whisper-1", file=buf),
    )
    return transcript.text


async def _build_user_content(update: Update, bot) -> list[dict] | str | None:
    """Extract user content from a Telegram message: text, images, docs, voice, stickers, location.

    Returns a list of content blocks for multimodal, a plain string for text-only, or None if nothing useful.
    """
    msg = update.message
    content_blocks = []
    text = msg.text or msg.caption or ""

    # Photos (Telegram sends multiple sizes; take the largest)
    if msg.photo:
        try:
            photo = msg.photo[-1]  # highest resolution
            data = await _download_telegram_file(photo, bot)
            content_blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(data).decode()},
                }
            )
        except Exception as e:
            logger.warning("Failed to download photo: %s", e)
            text += "\n[Photo attached but could not be downloaded]"

    # Stickers → treat as image
    if msg.sticker and not msg.sticker.is_animated and not msg.sticker.is_video:
        try:
            data = await _download_telegram_file(msg.sticker, bot)
            media_type = "image/webp"
            content_blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
                }
            )
            if not text:
                text = f"[Sticker: {msg.sticker.emoji or 'unknown'}]"
        except Exception as e:
            logger.warning("Failed to download sticker: %s", e)

    # Documents (images, PDFs, or text files sent as attachments)
    if msg.document:
        mime = msg.document.mime_type or ""
        fname = msg.document.file_name or "file"
        try:
            if mime in _SUPPORTED_DOC_MIMES:
                data = await _download_telegram_file(msg.document, bot)
                if mime in _IMAGE_MIME_TYPES:
                    content_blocks.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()},
                        }
                    )
                elif mime == "application/pdf":
                    content_blocks.append(
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.b64encode(data).decode(),
                            },
                        }
                    )
            elif mime.startswith("text/") or fname.endswith(
                (
                    ".txt",
                    ".py",
                    ".js",
                    ".ts",
                    ".json",
                    ".md",
                    ".csv",
                    ".yaml",
                    ".yml",
                    ".toml",
                    ".xml",
                    ".html",
                    ".css",
                    ".sh",
                    ".rs",
                    ".go",
                    ".java",
                    ".c",
                    ".cpp",
                    ".h",
                    ".rb",
                    ".sql",
                    ".log",
                )
            ):
                data = await _download_telegram_file(msg.document, bot)
                file_text = data.decode("utf-8", errors="replace")
                if len(file_text) > MAX_CONTENT_SIZE:
                    file_text = file_text[:MAX_CONTENT_SIZE] + "\n... (truncated)"
                text += f"\n\n--- Attached file: {fname} ---\n{file_text}"
            else:
                text += f"\n[Attached file: {fname} ({mime}) — unsupported format]"
        except Exception as e:
            logger.warning("Failed to download document %s: %s", fname, e)
            text += f"\n[Attached file: {fname} — download failed]"

    # Voice messages
    if msg.voice:
        if openai_client:
            try:
                transcript = await _transcribe_voice(msg.voice, bot, "voice.ogg")
                text += f"\n[Voice transcription]: {transcript}"
            except Exception as e:
                logger.warning("Voice transcription failed: %s", e)
                text += "\n[Voice message received — transcription failed. Please type your message instead.]"
        else:
            text += "\n[Voice message received — voice transcription not configured. Please type your message instead.]"

    # Audio files
    if msg.audio:
        if openai_client:
            try:
                fname = msg.audio.file_name or "audio.mp3"
                transcript = await _transcribe_voice(msg.audio, bot, fname)
                text += f"\n[Audio transcription of {fname}]: {transcript}"
            except Exception as e:
                logger.warning("Audio transcription failed: %s", e)
                text += f"\n[Audio file: {msg.audio.title or msg.audio.file_name or 'audio'} — transcription failed]"
        else:
            text += (
                f"\n[Audio file: {msg.audio.title or msg.audio.file_name or 'audio'} — transcription not configured]"
            )

    # Video
    if msg.video:
        text += f"\n[Video attached ({msg.video.duration}s) — video processing not supported]"

    # Video notes (circle videos)
    if msg.video_note:
        text += "\n[Video note attached — video processing not supported]"

    # Location
    if msg.location:
        lat, lon = msg.location.latitude, msg.location.longitude
        text += f"\n[Location shared: {lat}, {lon}]"

    # Contact
    if msg.contact:
        c = msg.contact
        parts = [c.first_name or "", c.last_name or ""]
        name = " ".join(p for p in parts if p)
        text += f"\n[Contact shared: {name}, {c.phone_number or 'no phone'}]"

    # Build final content
    if content_blocks:
        if text:
            content_blocks.append({"type": "text", "text": text})
        return content_blocks
    elif text:
        return text
    return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_authorized(update.effective_user.id):
        try:
            await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        except TelegramError:
            pass
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    raw_text = (update.message.text or "").strip()

    # Handle "!" as cancel/interrupt
    if raw_text == "!":
        cancel_event = _cancel_events.get(chat_id)
        if cancel_event:
            cancel_event.set()
            try:
                await update.message.reply_text("Cancelling...")
            except TelegramError:
                pass
        else:
            try:
                await update.message.reply_text("Nothing to cancel.")
            except TelegramError:
                pass
        return

    # Build user content from text + any attachments
    user_content = await _build_user_content(update, context.bot)
    if not user_content:
        return

    text_preview = (user_content[:80] + "...") if isinstance(user_content, str) and len(user_content) > 80 else ""
    audit_log(
        "message",
        chat_id=chat_id,
        user_id=user_id,
        detail=text_preview if isinstance(user_content, str) else "multimodal",
    )

    lock = _chat_locks[chat_id]
    if lock.locked():
        try:
            await update.message.reply_text("Queued — I'll get to this once I finish the current request.")
        except TelegramError:
            pass

    cancel = asyncio.Event()
    async with lock:
        _cancel_events[chat_id] = cancel
        try:
            await _process_message(chat_id, user_content, update, context, cancel=cancel)
        finally:
            _cancel_events.pop(chat_id, None)


async def _process_message(
    chat_id: int,
    user_content,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cancel: asyncio.Event | None = None,
) -> None:
    """Core message processing, runs under per-chat lock.

    user_content can be a plain string or a list of content blocks (multimodal).
    If cancel event is set, the current request is abandoned and history rolled back.
    """
    bot = context.bot
    history = get_conversation(chat_id)
    history_len_before = len(history)
    history.append({"role": "user", "content": user_content})
    trim_history(chat_id)

    repo = get_active_repo(chat_id)
    tools = [TODO_TOOL, ASK_USER_TOOL, SCHEDULE_CHECK_TOOL, MANAGE_PULSE_TOOL]
    if gh_client:
        tools.extend(GITHUB_TOOLS)
    if web_client:
        tools.extend(WEB_TOOLS)
    if tasks_client:
        tools.extend(TASKS_TOOLS)
    if calendar_client:
        tools.extend(CALENDAR_TOOLS)
    if email_client:
        tools.extend(EMAIL_TOOLS)
    if contacts_client:
        tools.extend(CONTACTS_TOOLS)
    if train_client:
        tools.extend(TRAIN_TOOLS)
    if MCP_TOOLS:
        tools.extend(MCP_TOOLS)

    try:
        import zoneinfo

        tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.UTC
    now = datetime.datetime.now(tz)
    date_str = now.strftime("%A, %B %d, %Y at %I:%M %p")
    # Pre-compute upcoming days so the model doesn't do bad date math
    upcoming = []
    for i in range(1, 8):
        d = now + datetime.timedelta(days=i)
        upcoming.append(d.strftime("%A %b %d"))
    upcoming_str = ", ".join(upcoming)
    system = (
        f"TODAY IS {date_str} ({USER_TIMEZONE}). "
        f"Coming days: {upcoming_str}. "
        "These dates are AUTHORITATIVE — use them for ALL date references. "
        "Ignore any conflicting dates from earlier messages in the conversation history.\n\n"
        + SYSTEM_PROMPT
        + f"\n\nModel: {get_model(chat_id)}"
    )
    if repo:
        branch = get_active_branch(chat_id)
        system += f"\n\nActive repository: {repo}"
        if branch:
            system += (
                f"\nActive branch: {branch} — use this branch for file changes unless the user specifies otherwise."
            )

    # Plan mode: inject existing todos and planning instructions
    if get_plan_mode(chat_id):
        todos = get_todos(chat_id)
        if todos:
            system += f"\n\nCurrent todo list:\n{format_todo_list(todos)}"
        system += (
            "\n\nPLAN MODE IS ON. Before making any changes (file edits, PRs, emails, etc.), "
            "first outline a numbered plan of what you intend to do and ask the user to confirm. "
            "Only proceed after they approve. Use the update_todo_list tool to track the plan steps."
        )

    max_rounds = MAX_TOOL_ROUNDS

    # Shared progress status — the tool loop writes, keep_typing reads
    progress: dict[str, Any] = {"round": 0, "max": max_rounds, "tools": [], "last_update_round": -1}

    # Start typing indicator in background
    stop_typing = asyncio.Event()
    start_time = time.time()
    typing_task = asyncio.create_task(keep_typing(update.effective_chat, stop_typing, start_time, bot, progress))

    try:
        for round_num in range(max_rounds):
            # Check for cancel before each round
            if cancel and cancel.is_set():
                del history[history_len_before:]
                save_state(chat_id)
                stop_typing.set()
                await typing_task
                await send_long_message(chat_id, "Request cancelled.", bot)
                return

            progress["round"] = round_num + 1

            # Sanitize before each API call to catch any mid-session corruption
            # (e.g. orphaned tool_use/tool_result from errors in previous rounds)
            sanitized_messages = _sanitize_history(history)
            if len(sanitized_messages) != len(history):
                logger.warning(
                    "Pre-call sanitization fixed history: %d -> %d messages",
                    len(history),
                    len(sanitized_messages),
                )
                history.clear()
                history.extend(sanitized_messages)

            kwargs: dict[str, Any] = {
                "model": get_model(chat_id),
                "max_tokens": 4096,
                "system": system,
                "messages": history,
            }
            if tools:
                kwargs["tools"] = tools
            # Enable extended thinking on first round if user requested it
            if round_num == 0 and _wants_extended_thinking(user_content):
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10000}
                kwargs["max_tokens"] = 16000

            # Attempt streaming; fall back to non-streaming on transient errors
            streamed_text = None
            try:
                response, streamed_text = await _stream_round(kwargs, chat_id, bot, stop_typing, cancel=cancel)
            except asyncio.CancelledError:
                del history[history_len_before:]
                save_state(chat_id)
                stop_typing.set()
                await typing_task
                await send_long_message(chat_id, "Request cancelled.", bot)
                return
            except (anthropic.RateLimitError, anthropic.InternalServerError) as stream_err:
                logger.warning("Streaming failed (%s), falling back to non-streaming", stream_err)
                response = await _call_anthropic(**kwargs)

            if response.stop_reason != "tool_use":
                history.append({"role": "assistant", "content": response.content})
                save_state(chat_id)
                stop_typing.set()
                await typing_task
                if streamed_text is None:
                    # Non-streaming fallback
                    text_parts = [b.text for b in response.content if b.type == "text"]
                    reply = "\n".join(text_parts) if text_parts else "(no response)"
                    await send_long_message(chat_id, reply, bot)
                return

            # Tool use round
            history.append({"role": "assistant", "content": response.content})

            tool_results = []
            loop = asyncio.get_running_loop()
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Tool call [%d]: %s(%s)", round_num + 1, block.name, json.dumps(block.input)[:200])
                    progress["tools"].append(block.name)
                    if block.name == "ask_user":
                        result = await _handle_ask_user(block, chat_id, bot)
                    elif block.name.startswith("mcp_") and mcp_manager:
                        result = await mcp_manager.call_tool(block.name, block.input)
                    else:
                        result = await loop.run_in_executor(None, _execute_tool_call, block, repo, chat_id)
                    if len(result) > 10000:
                        result = result[:10000] + "\n... (truncated)"
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            history.append({"role": "user", "content": tool_results})
            # Save after each tool round in case of crash
            save_state(chat_id)

            # Register any pulse jobs requested during this tool round
            if _pending_pulse_registrations or _pending_pulse_unregistrations:
                job_queue = bot.application.job_queue if hasattr(bot, "application") else None
                if job_queue:
                    while _pending_pulse_unregistrations:
                        _unregister_pulse(_pending_pulse_unregistrations.pop(0))
                    while _pending_pulse_registrations:
                        _register_pulse(job_queue, _pending_pulse_registrations.pop(0))

            # Register any monitors created during this tool round
            if _pending_monitor_registrations:
                job_queue = bot.application.job_queue if hasattr(bot, "application") else None
                while _pending_monitor_registrations:
                    monitor = _pending_monitor_registrations.pop(0)
                    if job_queue:
                        _register_monitor(job_queue, monitor, bot)

        stop_typing.set()
        await typing_task
        await send_long_message(chat_id, "(Reached tool call limit. Send another message to continue.)", bot)

    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        # Roll back all messages added during this request
        conversations[chat_id] = history[:history_len_before]
        save_state(chat_id)
        stop_typing.set()
        await typing_task
        msg = f"Claude API error: {getattr(e, 'message', str(e))}"
        await send_long_message(chat_id, msg, bot)
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        # Roll back all messages added during this request
        conversations[chat_id] = history[:history_len_before]
        save_state(chat_id)
        stop_typing.set()
        await typing_task
        await send_long_message(chat_id, "Something went wrong. Please try again.", bot)


# ── Monitor execution ─────────────────────────────────────────────────


def _register_monitor(job_queue, monitor: dict, bot=None) -> None:
    """Register a monitor dict as a repeating JobQueue job."""
    monitor_id = monitor["id"]
    job_data = {**monitor, "bot": bot}
    job_name = f"monitor_{monitor_id}"
    interval = monitor["interval_minutes"] * 60

    job = job_queue.run_repeating(
        _run_monitor_job,
        interval=interval,
        first=10,  # first check 10s after creation
        data=job_data,
        name=job_name,
    )
    _monitor_jobs[monitor_id] = job
    logger.info("Registered monitor #%d: every %dm — %s", monitor_id, monitor["interval_minutes"], monitor["summary"])


def _unregister_monitor(monitor_id: int) -> None:
    """Remove a monitor job from the job queue."""
    job = _monitor_jobs.pop(monitor_id, None)
    if job:
        job.schedule_removal()


async def _run_monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback for monitor checks."""
    job_data: dict = context.job.data  # type: ignore[assignment]
    monitor_id = job_data["id"]
    chat_id = job_data["chat_id"]
    check_prompt = job_data["check_prompt"]
    notify_condition = job_data["notify_condition"]
    summary = job_data["summary"]
    expires_at = job_data["expires_at"]
    last_result = job_data.get("last_result")

    # Check expiry
    if time.time() > expires_at:
        _unregister_monitor(monitor_id)
        disable_monitor(monitor_id)
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"Monitor expired: {summary}")
        except Exception:
            pass
        logger.info("Monitor #%d expired: %s", monitor_id, summary)
        return

    try:
        # Run the check prompt through the tool loop to get current state
        current_result = await _run_monitor_prompt(context.bot, chat_id, check_prompt)

        if last_result is None:
            # First run — capture baseline silently
            job_data["last_result"] = current_result
            update_monitor_result(monitor_id, current_result)
            logger.info("Monitor #%d baseline captured", monitor_id)
            return

        # Compare old vs new
        should_notify, alert_msg = await _compare_monitor_results(
            last_result, current_result, notify_condition, summary
        )

        # Update stored result
        job_data["last_result"] = current_result
        update_monitor_result(monitor_id, current_result)

        if should_notify and alert_msg:
            await send_long_message(chat_id, f"🔔 {summary}\n\n{alert_msg}", context.bot)
            audit_log("monitor_alert", chat_id=chat_id, detail=f"#{monitor_id}: {summary}")

    except Exception as e:
        logger.warning("Monitor #%d check failed: %s", monitor_id, e)


async def _run_monitor_prompt(bot, chat_id: int, prompt: str) -> str:
    """Run a monitor check prompt through the tool loop, returning the text result.

    Similar to run_scheduled_prompt but returns text instead of sending it.
    """
    tools: list[dict[str, Any]] = []
    if gh_client:
        tools.extend(GITHUB_TOOLS)
    if web_client:
        tools.extend(WEB_TOOLS)
    if tasks_client:
        tools.extend(TASKS_TOOLS)
    if calendar_client:
        tools.extend(CALENDAR_TOOLS)
    if contacts_client:
        tools.extend(CONTACTS_TOOLS)
    if train_client:
        tools.extend(TRAIN_TOOLS)

    try:
        import zoneinfo

        tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.UTC
    now = datetime.datetime.now(tz)

    system = (
        f"Today is {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({USER_TIMEZONE}).\n\n"
        "You are running a background monitoring check. Gather the requested data using the "
        "available tools and return a concise factual summary of the current state. "
        "Do NOT address the user — just report the data."
    )

    repo = get_active_repo(chat_id)
    if repo:
        system += f"\n\nActive repository: {repo}"

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    loop = asyncio.get_running_loop()

    for _ in range(5):  # fewer rounds than interactive — monitors should be quick
        response = await _call_anthropic(
            model="claude-haiku-4-5-20251001",  # use Haiku for cost efficiency
            max_tokens=1024,
            system=system,
            messages=messages,
            **({"tools": tools} if tools else {}),
        )

        if response.stop_reason != "tool_use":
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts) if text_parts else "(no data)"

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = await loop.run_in_executor(None, _execute_tool_call, block, repo, chat_id)
                except Exception as e:
                    result = f"Tool error: {e}"
                if len(result) > 10000:
                    result = result[:10000] + "\n... (truncated)"
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    return "(monitor check hit tool limit)"


async def _compare_monitor_results(
    previous: str, current: str, notify_condition: str, summary: str
) -> tuple[bool, str | None]:
    """Use Claude to compare two monitor snapshots and decide whether to notify.

    Returns (should_notify, alert_message_or_none).
    """
    prompt = (
        f"You are comparing two snapshots from a background monitor: '{summary}'.\n\n"
        f"PREVIOUS STATE:\n{previous[:3000]}\n\n"
        f"CURRENT STATE:\n{current[:3000]}\n\n"
        f"NOTIFY CONDITION: {notify_condition}\n\n"
        "Does the current state meet the notify condition (compared to the previous state)?\n"
        "If YES: write a concise alert message for a phone notification (2-3 lines max).\n"
        "If NO: respond with exactly the word NO_CHANGE and nothing else."
    )

    response = await _call_anthropic(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    text_parts = [b.text for b in response.content if b.type == "text"]
    reply = "\n".join(text_parts).strip()

    if reply == "NO_CHANGE" or reply.startswith("NO_CHANGE"):
        return False, None
    return True, reply


# ── Pulse: autonomous agent ───────────────────────────────────────────


def _handle_manage_pulse(tool_input: dict, chat_id: int) -> str:
    """Handle the manage_pulse tool call."""
    action = tool_input.get("action", "")

    if action == "add_goal":
        goal_text = tool_input.get("goal", "").strip()
        if not goal_text:
            return "Error: goal text is required."
        priority = tool_input.get("priority", "normal")
        # Ensure config exists
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=60, quiet_start=None, quiet_end=None)
        goal_id = save_pulse_goal(chat_id, goal_text, priority)
        return f"Goal #{goal_id} added: {goal_text} (priority: {priority})"

    if action == "remove_goal":
        remove_id = tool_input.get("goal_id")
        if remove_id is None:
            return "Error: goal_id is required."
        if delete_pulse_goal(int(remove_id), chat_id):
            return f"Goal #{remove_id} removed."
        return f"Goal #{remove_id} not found."

    if action == "list_goals":
        goals = load_pulse_goals(chat_id)
        if not goals:
            return "No pulse goals configured. Add some with add_goal."
        lines = []
        for g in goals:
            lines.append(f"#{g['id']} [{g['priority']}] {g['goal']}")
        return "\n".join(lines)

    if action == "enable":
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=True,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)  # invalidate cache
        _pending_pulse_registrations.append(chat_id)
        goals = load_pulse_goals(chat_id)
        if not goals:
            return "Pulse enabled, but no goals configured yet. Add goals so Pulse knows what to watch."
        return "Pulse enabled. It will start checking on the next interval."

    if action == "disable":
        config = load_pulse_config(chat_id)
        if config:
            save_pulse_config(
                chat_id,
                enabled=False,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        _pending_pulse_unregistrations.append(chat_id)
        return "Pulse disabled."

    if action == "set_interval":
        minutes = tool_input.get("interval_minutes", 60)
        minutes = max(15, min(240, int(minutes)))
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=minutes, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=minutes,
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        if config and config["enabled"]:
            _pending_pulse_registrations.append(chat_id)
        return f"Pulse interval set to {minutes} minutes."

    if action == "set_quiet_hours":
        quiet_start = tool_input.get("quiet_start")
        quiet_end = tool_input.get("quiet_end")
        if not quiet_start or not quiet_end:
            return "Error: both quiet_start and quiet_end are required (e.g. '22:00' and '07:00')."
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=60, quiet_start=quiet_start, quiet_end=quiet_end)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=config["interval_minutes"],
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )
        _pulse_configs.pop(chat_id, None)
        return f"Quiet hours set: {quiet_start} - {quiet_end}."

    if action == "status":
        config = load_pulse_config(chat_id)
        if not config:
            return "Pulse is not configured. Enable it and add goals to get started."
        goals = load_pulse_goals(chat_id)
        lines = [
            f"Enabled: {'yes' if config['enabled'] else 'no'}",
            f"Interval: every {config['interval_minutes']}m",
        ]
        if config["quiet_start"] and config["quiet_end"]:
            lines.append(f"Quiet hours: {config['quiet_start']} - {config['quiet_end']}")
        if config["last_pulse_at"]:
            try:
                import zoneinfo

                tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
            except Exception:
                tz = datetime.UTC
            last_dt = datetime.datetime.fromtimestamp(config["last_pulse_at"], tz=tz)
            lines.append(f"Last pulse: {last_dt.strftime('%H:%M %b %d')}")
        lines.append(f"Goals: {len(goals)}")
        for g in goals:
            lines.append(f"  #{g['id']} [{g['priority']}] {g['goal']}")
        return "\n".join(lines)

    return f"Unknown action: {action}"


# Pending pulse registrations/unregistrations from sync tool call → picked up by async _process_message
_pending_pulse_registrations: list[int] = []
_pending_pulse_unregistrations: list[int] = []


def _is_quiet_hours(quiet_start: str | None, quiet_end: str | None, tz: datetime.tzinfo) -> bool:
    """Check if current time is within quiet hours."""
    if not quiet_start or not quiet_end:
        return False
    now = datetime.datetime.now(tz)
    try:
        start_h, start_m = (int(x) for x in quiet_start.split(":"))
        end_h, end_m = (int(x) for x in quiet_end.split(":"))
    except (ValueError, AttributeError):
        return False
    current_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes <= end_minutes:
        # Same day: e.g. 09:00 - 17:00
        return start_minutes <= current_minutes < end_minutes
    else:
        # Overnight: e.g. 22:00 - 07:00
        return current_minutes >= start_minutes or current_minutes < end_minutes


async def _build_triage_context(chat_id: int) -> str:
    """Build a compact context snapshot for pulse triage (~500 tokens)."""
    parts = []
    loop = asyncio.get_running_loop()

    try:
        import zoneinfo

        tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.UTC
    now = datetime.datetime.now(tz)
    parts.append(f"Time: {now.strftime('%A %H:%M %Z')}")

    # Goals
    goals = load_pulse_goals(chat_id)
    if goals:
        goal_lines = [f"- [{g['priority']}] {g['goal']}" for g in goals]
        parts.append("Goals:\n" + "\n".join(goal_lines))

    # Calendar (next 4 hours)
    if calendar_client and execute_calendar_tool:
        try:
            time_min = now.isoformat()
            time_max = (now + datetime.timedelta(hours=4)).isoformat()
            result = await loop.run_in_executor(
                None,
                execute_calendar_tool,
                calendar_client,
                "list_events",
                {"time_min": time_min, "time_max": time_max, "max_results": 5},
            )
            parts.append(f"Calendar (next 4h): {result[:500]}")
        except Exception as e:
            logger.debug("Pulse triage calendar fetch failed: %s", e)

    # Tasks
    if tasks_client and execute_tasks_tool:
        try:
            result = await loop.run_in_executor(
                None, execute_tasks_tool, tasks_client, "list_tasks", {"max_results": 10}
            )
            parts.append(f"Tasks: {result[:500]}")
        except Exception as e:
            logger.debug("Pulse triage tasks fetch failed: %s", e)

    # Todos
    todos = get_todos(chat_id)
    if todos:
        pending = [t for t in todos if t.get("status") != "completed"]
        if pending:
            parts.append(f"Todos: {format_todo_list(pending)}")

    # Last pulse summary
    config = load_pulse_config(chat_id)
    if config and config.get("last_pulse_summary"):
        parts.append(f"Last pulse said: {config['last_pulse_summary'][:300]}")

    return "\n\n".join(parts)


async def _run_pulse_triage(chat_id: int) -> dict:
    """Run triage with Haiku. Returns {"act": bool, "reason": str}."""
    context_text = await _build_triage_context(chat_id)

    triage_prompt = (
        "You are a triage agent for a personal assistant. Review this context snapshot and decide "
        "if there's anything worth proactively telling the user about RIGHT NOW.\n\n"
        "Consider:\n"
        "- Upcoming events they should prepare for\n"
        "- Overdue or urgent tasks\n"
        "- Things matching their stated goals\n"
        "- Time-sensitive information\n\n"
        "Be conservative — silence is better than noise. Only recommend action if there's genuine value.\n"
        "If the last pulse already covered this information, don't repeat it.\n\n"
        f"CONTEXT:\n{context_text}\n\n"
        'Respond with ONLY valid JSON: {"act": true/false, "reason": "brief reason or empty"}'
    )

    try:
        response = await _call_anthropic(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": triage_prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        # Parse JSON from response (handle markdown code blocks)
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return {"act": bool(result.get("act", False)), "reason": result.get("reason", "")}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Pulse triage JSON parse failed: %s — raw: %s", e, text[:200] if "text" in dir() else "n/a")
        return {"act": False, "reason": ""}
    except Exception as e:
        logger.warning("Pulse triage failed: %s", e)
        return {"act": False, "reason": ""}


async def _run_pulse_action(bot, chat_id: int, triage_result: dict) -> None:
    """Run the action phase with Sonnet + full tools. Sends result to user."""
    goals = load_pulse_goals(chat_id)
    config = load_pulse_config(chat_id)
    goal_text = "\n".join(f"- [{g['priority']}] {g['goal']}" for g in goals) if goals else "(no specific goals)"
    last_summary = config.get("last_pulse_summary", "") if config else ""

    try:
        import zoneinfo

        tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.UTC
    now = datetime.datetime.now(tz)

    action_prompt = (
        f"You are Teleclaude's Pulse agent running a proactive check at {now.strftime('%H:%M %Z')}.\n\n"
        f"TRIAGE REASON: {triage_result.get('reason', 'General check')}\n\n"
        f"USER'S GOALS:\n{goal_text}\n\n"
        + (f"LAST PULSE SAID: {last_summary[:500]}\n\n" if last_summary else "")
        + "Use the available tools to gather current information relevant to the triage reason and goals. "
        "Then compose a concise, helpful update for the user's phone screen.\n\n"
        "Guidelines:\n"
        "- Be brief — 2-5 lines max unless there's a lot to report.\n"
        "- Don't repeat info from the last pulse unless it has changed.\n"
        "- Actionable > informational.\n"
        "- End with a one-line summary of what you checked (this will be stored as context for next pulse)."
    )

    # Build tools
    tools: list[dict[str, Any]] = []
    if gh_client:
        tools.extend(GITHUB_TOOLS)
    if web_client:
        tools.extend(WEB_TOOLS)
    if tasks_client:
        tools.extend(TASKS_TOOLS)
    if calendar_client:
        tools.extend(CALENDAR_TOOLS)
    if email_client:
        tools.extend(EMAIL_TOOLS)
    if contacts_client:
        tools.extend(CONTACTS_TOOLS)
    if train_client:
        tools.extend(TRAIN_TOOLS)

    system = (
        f"Today is {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({USER_TIMEZONE}).\n\n"
        "You are running as Teleclaude's Pulse agent — a proactive background check. "
        "Keep responses concise and useful for a phone screen."
    )

    repo = get_active_repo(chat_id)
    if repo:
        system += f"\n\nActive repository: {repo}"

    messages: list[dict[str, Any]] = [{"role": "user", "content": action_prompt}]
    loop = asyncio.get_running_loop()

    for _ in range(8):
        response = await _call_anthropic(
            model=DEFAULT_MODEL,
            max_tokens=2048,
            system=system,
            messages=messages,
            **({"tools": tools} if tools else {}),
        )

        if response.stop_reason != "tool_use":
            text_parts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_parts) if text_parts else "(Pulse check completed with no output.)"
            await send_long_message(chat_id, f"Pulse\n\n{reply}", bot)
            # Store summary (last line or truncated reply)
            summary = reply.split("\n")[-1][:300] if reply else ""
            update_pulse_last_run(chat_id, summary)
            _pulse_configs.pop(chat_id, None)  # invalidate cache
            audit_log("pulse_action", chat_id=chat_id, detail=summary[:100])
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = await loop.run_in_executor(None, _execute_tool_call, block, repo, chat_id)
                except Exception as e:
                    result = f"Tool error: {e}"
                if len(result) > 10000:
                    result = result[:10000] + "\n... (truncated)"
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    await send_long_message(chat_id, "Pulse check hit tool limit.", bot)


async def _run_pulse(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback for pulse checks."""
    job_data: dict = context.job.data  # type: ignore[assignment]
    chat_id = job_data["chat_id"]

    # Reload config in case it changed
    config = load_pulse_config(chat_id)
    if not config or not config["enabled"]:
        return

    try:
        import zoneinfo

        tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.UTC

    # Check quiet hours
    if _is_quiet_hours(config.get("quiet_start"), config.get("quiet_end"), tz):
        logger.debug("Pulse for chat %d skipped — quiet hours", chat_id)
        return

    # Check goals exist
    goals = load_pulse_goals(chat_id)
    if not goals:
        logger.debug("Pulse for chat %d skipped — no goals", chat_id)
        return

    logger.info("Pulse triage starting for chat %d", chat_id)

    # Phase 1: Triage (cheap)
    triage = await _run_pulse_triage(chat_id)
    if not triage.get("act"):
        logger.info("Pulse triage for chat %d: no action needed", chat_id)
        return

    # Phase 2: Action (full tools)
    logger.info("Pulse action for chat %d: %s", chat_id, triage.get("reason", "")[:80])
    try:
        await _run_pulse_action(context.bot, chat_id, triage)
    except Exception as e:
        logger.warning("Pulse action failed for chat %d: %s", chat_id, e)


def _register_pulse(job_queue, chat_id: int) -> None:
    """Register a pulse job for a chat."""
    # Unregister existing first
    _unregister_pulse(chat_id)

    config = load_pulse_config(chat_id)
    if not config or not config["enabled"]:
        return

    interval = config["interval_minutes"] * 60
    job_data = {"chat_id": chat_id}
    job_name = f"pulse_{chat_id}"

    job = job_queue.run_repeating(
        _run_pulse,
        interval=interval,
        first=30,  # first check 30s after registration
        data=job_data,
        name=job_name,
    )
    _pulse_jobs[chat_id] = job
    logger.info("Registered pulse for chat %d: every %dm", chat_id, config["interval_minutes"])


def _unregister_pulse(chat_id: int) -> None:
    """Remove a pulse job from the job queue."""
    job = _pulse_jobs.pop(chat_id, None)
    if job:
        job.schedule_removal()


async def pulse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pulse command handler."""
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        # Show status
        config = load_pulse_config(chat_id)
        goals = load_pulse_goals(chat_id)
        if not config:
            await update.message.reply_text(
                "Pulse is not configured yet.\n\n"
                "Pulse is an autonomous agent that periodically checks your context "
                "(calendar, tasks, goals) and sends updates when something needs attention.\n\n"
                "Get started:\n"
                "/pulse on — enable Pulse\n"
                "Then tell me what to watch for, e.g.:\n"
                '"Keep an eye on my PRs"\n'
                '"Remind me about overdue tasks"\n'
                '"Watch for calendar conflicts"'
            )
            return

        lines = [f"Pulse: {'ON' if config['enabled'] else 'OFF'}"]
        lines.append(f"Interval: every {config['interval_minutes']}m")
        if config["quiet_start"] and config["quiet_end"]:
            lines.append(f"Quiet hours: {config['quiet_start']} - {config['quiet_end']}")
        if config.get("last_pulse_at"):
            try:
                import zoneinfo

                tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
            except Exception:
                tz = datetime.UTC
            last_dt = datetime.datetime.fromtimestamp(config["last_pulse_at"], tz=tz)
            lines.append(f"Last active pulse: {last_dt.strftime('%H:%M %b %d')}")

        if goals:
            lines.append(f"\nGoals ({len(goals)}):")
            for g in goals:
                lines.append(f"  #{g['id']} [{g['priority']}] {g['goal']}")
        else:
            lines.append("\nNo goals. Tell me what to watch for.")

        await update.message.reply_text("\n".join(lines))
        return

    subcmd = args[0].lower()

    if subcmd in ("on", "enable"):
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=True,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        if context.application.job_queue:
            _register_pulse(context.application.job_queue, chat_id)
        goals = load_pulse_goals(chat_id)
        if goals:
            await update.message.reply_text("Pulse enabled.")
        else:
            await update.message.reply_text("Pulse enabled. Now tell me what to watch for.")
        return

    if subcmd in ("off", "disable"):
        config = load_pulse_config(chat_id)
        if config:
            save_pulse_config(
                chat_id,
                enabled=False,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        _unregister_pulse(chat_id)
        await update.message.reply_text("Pulse disabled.")
        return

    if subcmd == "every":
        if len(args) < 2:
            await update.message.reply_text("Usage: /pulse every 30m or /pulse every 2h")
            return
        interval_str = args[1].lower()
        match = re.match(r"^(\d+)(m|h)$", interval_str)
        if not match:
            await update.message.reply_text("Invalid interval. Use e.g. 30m or 2h.")
            return
        value = int(match.group(1))
        unit = match.group(2)
        minutes = value if unit == "m" else value * 60
        minutes = max(15, min(240, minutes))
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=minutes, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=minutes,
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        if config and config["enabled"] and context.application.job_queue:
            _register_pulse(context.application.job_queue, chat_id)
        await update.message.reply_text(f"Pulse interval set to {minutes} minutes.")
        return

    if subcmd == "quiet":
        if len(args) < 2:
            await update.message.reply_text("Usage: /pulse quiet 22:00-07:00")
            return
        time_range = args[1]
        parts = time_range.split("-")
        if len(parts) != 2:
            await update.message.reply_text("Usage: /pulse quiet 22:00-07:00")
            return
        quiet_start, quiet_end = parts[0].strip(), parts[1].strip()
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=60, quiet_start=quiet_start, quiet_end=quiet_end)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=config["interval_minutes"],
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )
        _pulse_configs.pop(chat_id, None)
        await update.message.reply_text(f"Quiet hours set: {quiet_start} - {quiet_end}")
        return

    if subcmd == "goals":
        goals = load_pulse_goals(chat_id)
        if not goals:
            await update.message.reply_text("No goals. Tell me what to watch for.")
        else:
            lines = [f"#{g['id']} [{g['priority']}] {g['goal']}" for g in goals]
            await update.message.reply_text("\n".join(lines))
        return

    if subcmd == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /pulse remove <goal_id>")
            return
        try:
            goal_id = int(args[1].lstrip("#"))
        except ValueError:
            await update.message.reply_text("Invalid goal ID.")
            return
        if delete_pulse_goal(goal_id, chat_id):
            await update.message.reply_text(f"Goal #{goal_id} removed.")
        else:
            await update.message.reply_text(f"Goal #{goal_id} not found.")
        return

    await update.message.reply_text(
        "Usage:\n"
        "/pulse — show status\n"
        "/pulse on/off — enable/disable\n"
        "/pulse every 30m — set interval\n"
        "/pulse quiet 22:00-07:00 — set quiet hours\n"
        "/pulse goals — list goals\n"
        "/pulse remove <id> — remove a goal"
    )


async def run_scheduled_prompt(bot, chat_id: int, prompt: str) -> None:
    """Run a prompt through the tool loop with all enabled tools. No conversation history."""
    tools: list[dict[str, Any]] = []
    if gh_client:
        tools.extend(GITHUB_TOOLS)
    if web_client:
        tools.extend(WEB_TOOLS)
    if tasks_client:
        tools.extend(TASKS_TOOLS)
    if calendar_client:
        tools.extend(CALENDAR_TOOLS)
    if email_client:
        tools.extend(EMAIL_TOOLS)
    if contacts_client:
        tools.extend(CONTACTS_TOOLS)
    if train_client:
        tools.extend(TRAIN_TOOLS)

    try:
        import zoneinfo

        tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.UTC
    now = datetime.datetime.now(tz)

    system = (
        f"Today is {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({USER_TIMEZONE}).\n\n"
        "You are Teleclaude running a scheduled prompt. Be concise and useful. "
        "Keep responses short and scannable for a phone screen."
    )

    repo = get_active_repo(chat_id)
    if repo:
        system += f"\n\nActive repository: {repo}"
        branch = get_active_branch(chat_id)
        if branch:
            system += f" (branch: {branch})"

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    loop = asyncio.get_running_loop()
    for _ in range(10):
        response = await _call_anthropic(
            model=DEFAULT_MODEL,
            max_tokens=2048,
            system=system,
            messages=messages,
            **({"tools": tools} if tools else {}),
        )

        if response.stop_reason != "tool_use":
            text_parts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_parts) if text_parts else "(No response from scheduled prompt.)"
            await send_long_message(chat_id, reply, bot)
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = await loop.run_in_executor(None, _execute_tool_call, block, repo, chat_id)
                except Exception as e:
                    result = f"Tool error: {e}"
                if len(result) > 10000:
                    result = result[:10000] + "\n... (truncated)"
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    await bot.send_message(chat_id=chat_id, text="Scheduled prompt hit tool limit.")


async def generate_briefing(bot, chat_id: int) -> None:
    """Generate and send a daily briefing using Claude with available tools."""
    briefing_prompt = (
        "Give me a concise morning briefing. Check my calendar for today's events, "
        "my task list for pending items, and search the web for today's weather in Chichester, UK. "
        "Format it as:\n"
        "- A quick summary line (e.g. '3 events, 5 tasks, 12°C partly cloudy')\n"
        "- Today's weather (temperature, conditions, rain chance)\n"
        "- Today's schedule in chronological order\n"
        "- Top pending tasks\n"
        "Keep it short and scannable for a phone screen."
    )
    await run_scheduled_prompt(bot, chat_id, briefing_prompt)


async def trigger_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual /briefing command."""
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text("Generating briefing...")
    try:
        await generate_briefing(context.bot, chat_id)
    except Exception as e:
        logger.error("Briefing error: %s", e, exc_info=True)
        await update.message.reply_text(f"Briefing failed: {e}")


async def _run_scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback for scheduled prompts."""
    job_data: dict = context.job.data  # type: ignore[assignment]
    chat_id = job_data["chat_id"]
    prompt = job_data["prompt"]
    try:
        await run_scheduled_prompt(context.bot, chat_id, prompt)
    except Exception as e:
        logger.warning("Scheduled job failed for chat %d: %s", chat_id, e)


def _register_schedule(job_queue, schedule: dict) -> None:
    """Register a schedule dict as a JobQueue job."""
    schedule_id = schedule["id"]
    chat_id = schedule["chat_id"]
    interval_type = schedule["interval_type"]
    interval_value = schedule["interval_value"]
    prompt = schedule["prompt"]
    job_data = {"chat_id": chat_id, "prompt": prompt}
    job_name = f"schedule_{schedule_id}"

    if interval_type == "daily":
        try:
            import zoneinfo

            tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
        except Exception:
            tz = datetime.UTC
        hour, minute = (int(x) for x in interval_value.split(":"))
        run_time = datetime.time(hour=hour, minute=minute, tzinfo=tz)
        job = job_queue.run_daily(_run_scheduled_job, time=run_time, data=job_data, name=job_name)
    elif interval_type == "every":
        # Parse "4h" -> 4 hours
        match = re.match(r"^(\d+)h$", interval_value)
        if not match:
            logger.warning("Invalid interval value for schedule %d: %s", schedule_id, interval_value)
            return
        hours = int(match.group(1))
        job = job_queue.run_repeating(_run_scheduled_job, interval=hours * 3600, data=job_data, name=job_name)
    else:
        logger.warning("Unknown interval_type for schedule %d: %s", schedule_id, interval_type)
        return

    _scheduled_jobs[schedule_id] = job
    logger.info("Registered schedule %d: %s %s — %s", schedule_id, interval_type, interval_value, prompt[:50])


def _unregister_schedule(schedule_id: int) -> None:
    """Remove a scheduled job from the job queue."""
    job = _scheduled_jobs.pop(schedule_id, None)
    if job:
        job.schedule_removal()


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/schedule command handler."""
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/schedule daily 08:00 Morning briefing\n"
            "/schedule every 4h Check my tasks\n"
            "/schedule list\n"
            "/schedule remove <id>"
        )
        return

    subcommand = args[0].lower()

    if subcommand == "list":
        schedules = load_schedules(chat_id)
        if not schedules:
            await update.message.reply_text("No schedules. Create one with /schedule daily or /schedule every.")
            return
        lines = []
        for s in schedules:
            if s["interval_type"] == "daily":
                timing = f"daily at {s['interval_value']}"
            else:
                timing = f"every {s['interval_value']}"
            lines.append(f"#{s['id']} — {timing}\n  {s['prompt']}")
        await update.message.reply_text("\n\n".join(lines))
        return

    if subcommand == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /schedule remove <id>")
            return
        try:
            schedule_id = int(args[1].lstrip("#"))
        except ValueError:
            await update.message.reply_text("Invalid schedule ID. Use /schedule list to see IDs.")
            return
        if delete_schedule(schedule_id, chat_id):
            _unregister_schedule(schedule_id)
            await update.message.reply_text(f"Schedule #{schedule_id} removed.")
        else:
            await update.message.reply_text(f"Schedule #{schedule_id} not found.")
        return

    if subcommand == "daily":
        if len(args) < 3:
            await update.message.reply_text("Usage: /schedule daily HH:MM <prompt>")
            return
        time_str = args[1]
        if not re.match(r"^\d{1,2}:\d{2}$", time_str):
            await update.message.reply_text("Invalid time format. Use HH:MM (e.g. 08:00).")
            return
        try:
            hour, minute = (int(x) for x in time_str.split(":"))
            datetime.time(hour=hour, minute=minute)  # validate
        except ValueError:
            await update.message.reply_text("Invalid time. Use HH:MM (e.g. 08:00, 18:30).")
            return
        prompt = " ".join(args[2:])
        schedule_id = save_schedule(chat_id, "daily", time_str, prompt)
        schedule_row = {
            "id": schedule_id,
            "chat_id": chat_id,
            "interval_type": "daily",
            "interval_value": time_str,
            "prompt": prompt,
        }
        if context.application.job_queue:
            _register_schedule(context.application.job_queue, schedule_row)
        await update.message.reply_text(f"Schedule #{schedule_id} created: daily at {time_str} ({USER_TIMEZONE})")
        return

    if subcommand == "every":
        if len(args) < 3:
            await update.message.reply_text("Usage: /schedule every Nh <prompt> (e.g. every 4h)")
            return
        interval = args[1].lower()
        if not re.match(r"^\d+h$", interval):
            await update.message.reply_text("Invalid interval. Use Nh format (e.g. 2h, 4h, 12h).")
            return
        hours = int(interval[:-1])
        if hours < 1 or hours > 24:
            await update.message.reply_text("Interval must be between 1h and 24h.")
            return
        prompt = " ".join(args[2:])
        schedule_id = save_schedule(chat_id, "every", interval, prompt)
        schedule_row = {
            "id": schedule_id,
            "chat_id": chat_id,
            "interval_type": "every",
            "interval_value": interval,
            "prompt": prompt,
        }
        if context.application.job_queue:
            _register_schedule(context.application.job_queue, schedule_row)
        await update.message.reply_text(f"Schedule #{schedule_id} created: every {interval}")
        return

    await update.message.reply_text(f"Unknown subcommand: {subcommand}\nUse: daily, every, list, remove")


async def monitors_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/monitors command handler — list and manage active monitors."""
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    args = context.args or []

    if not args or args[0].lower() == "list":
        monitors = load_monitors(chat_id)
        if not monitors:
            await update.message.reply_text("No active monitors. Ask me to watch something and I'll set one up.")
            return
        try:
            import zoneinfo

            tz: datetime.tzinfo = zoneinfo.ZoneInfo(USER_TIMEZONE)
        except Exception:
            tz = datetime.UTC
        lines = []
        for m in monitors:
            expires_dt = datetime.datetime.fromtimestamp(m["expires_at"], tz=tz)
            expires_str = (
                expires_dt.strftime("%H:%M")
                if expires_dt.date() == datetime.datetime.now(tz).date()
                else expires_dt.strftime("%b %d %H:%M")
            )
            lines.append(f"#{m['id']} — {m['summary']}\n  Every {m['interval_minutes']}m, expires {expires_str}")
        await update.message.reply_text("\n\n".join(lines))
        return

    if args[0].lower() == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /monitors remove <id>")
            return
        try:
            monitor_id = int(args[1].lstrip("#"))
        except ValueError:
            await update.message.reply_text("Invalid monitor ID.")
            return
        if delete_monitor(monitor_id, chat_id):
            _unregister_monitor(monitor_id)
            await update.message.reply_text(f"Monitor #{monitor_id} removed.")
        else:
            await update.message.reply_text(f"Monitor #{monitor_id} not found.")
        return

    await update.message.reply_text("Usage: /monitors [list] or /monitors remove <id>")


async def _load_schedules_on_startup(app: Application) -> None:
    """Load all saved schedules from DB and register with job_queue."""
    if not app.job_queue:
        logger.warning("JobQueue not available — schedules will not run")
        return

    # Auto-migrate: if DAILY_BRIEFING_TIME is set and no schedules exist, create one
    if DAILY_BRIEFING_TIME and ALLOWED_USER_IDS:
        all_schedules = load_all_schedules()
        if not all_schedules:
            briefing_prompt = (
                "Give me a concise morning briefing. Check my calendar for today's events, "
                "my task list for pending items, and search the web for today's weather in Chichester, UK. "
                "Format it as:\n"
                "- A quick summary line (e.g. '3 events, 5 tasks, 12°C partly cloudy')\n"
                "- Today's weather (temperature, conditions, rain chance)\n"
                "- Today's schedule in chronological order\n"
                "- Top pending tasks\n"
                "Keep it short and scannable for a phone screen."
            )
            for user_id in ALLOWED_USER_IDS:
                save_schedule(user_id, "daily", DAILY_BRIEFING_TIME, briefing_prompt)
            logger.info("Auto-created daily briefing schedules from DAILY_BRIEFING_TIME=%s", DAILY_BRIEFING_TIME)

    schedules = load_all_schedules()
    for s in schedules:
        try:
            _register_schedule(app.job_queue, s)
        except Exception as e:
            logger.warning("Failed to register schedule %d: %s", s["id"], e)

    if schedules:
        logger.info("Loaded %d schedule(s) from database", len(schedules))

    # Load active monitors
    monitors = load_all_monitors()
    now = time.time()
    for m in monitors:
        if m["expires_at"] <= now:
            # Already expired — clean up
            disable_monitor(m["id"])
            continue
        try:
            _register_monitor(app.job_queue, m)
        except Exception as e:
            logger.warning("Failed to register monitor %d: %s", m["id"], e)

    active_monitors = [m for m in monitors if m["expires_at"] > now]
    if active_monitors:
        logger.info("Loaded %d active monitor(s) from database", len(active_monitors))

    # Load active pulse configs
    pulse_configs = load_all_pulse_configs()
    for pc in pulse_configs:
        try:
            _register_pulse(app.job_queue, pc["chat_id"])
        except Exception as e:
            logger.warning("Failed to register pulse for chat %d: %s", pc["chat_id"], e)
    if pulse_configs:
        logger.info("Loaded %d pulse config(s) from database", len(pulse_configs))


async def _init_mcp() -> None:
    """Initialize MCP server connections (requires running event loop)."""
    global MCP_TOOLS
    if mcp_manager and _mcp_config:
        try:
            await mcp_manager.initialize(_mcp_config)
            MCP_TOOLS = mcp_manager.tools
            logger.info("MCP initialized: %d tool(s) available", len(MCP_TOOLS))
        except Exception as e:
            logger.warning("MCP initialization failed: %s", e)


async def notify_startup(app: Application) -> None:
    """Send a startup message to all allowed users."""
    await app.bot.set_my_commands(
        [
            ("new", "Start a new conversation"),
            ("repo", "Set active GitHub repo"),
            ("branch", "Set active branch"),
            ("model", "Show or change AI model"),
            ("plan", "Toggle plan mode"),
            ("briefing", "Get daily briefing"),
            ("todo", "Show current todo list"),
            ("schedule", "Manage scheduled jobs"),
            ("pulse", "Autonomous agent config"),
            ("monitors", "View active monitors"),
            ("logs", "View recent bot logs"),
            ("version", "Show bot version"),
            ("help", "Show help message"),
        ]
    )
    await _init_mcp()
    await _load_schedules_on_startup(app)

    if not ALLOWED_USER_IDS:
        logger.info("No ALLOWED_USER_IDS set — skipping startup notification")
        return

    integrations = []
    if gh_client:
        integrations.append("GitHub")
    if web_client:
        integrations.append("Web search")
    if tasks_client:
        integrations.append("Tasks")
    if calendar_client:
        integrations.append("Calendar")
    if email_client:
        integrations.append("Gmail")

    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    enabled = ", ".join(integrations) if integrations else "none"
    msg = f"Teleclaude v{VERSION} restarted at {now}\nModel: {DEFAULT_MODEL}\nIntegrations: {enabled}"

    for user_id in ALLOWED_USER_IDS:
        try:
            repo = get_active_repo(user_id)
            branch = get_active_branch(user_id)
            todos = get_todos(user_id)
            user_msg = msg
            if repo:
                repo_line = f"\nActive repo: {repo}"
                if branch:
                    repo_line += f" ({branch})"
                user_msg += repo_line
            if todos:
                pending = sum(1 for t in todos if t.get("status") != "completed")
                done = len(todos) - pending
                user_msg += f"\nTodos: {pending} pending, {done} done"
            await app.bot.send_message(chat_id=user_id, text=user_msg)
            logger.info("Sent startup notification to user %d", user_id)
        except Exception as e:
            logger.warning("Could not notify user %d: %s", user_id, e)


def main() -> None:
    _check_required_config()
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("repo", set_repo))
    app.add_handler(CommandHandler("branch", set_branch))
    app.add_handler(CommandHandler("new", new_conversation))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(CommandHandler("plan", toggle_plan))
    app.add_handler(CommandHandler("todo", show_todos))
    app.add_handler(CommandHandler("todos", show_todos))
    app.add_handler(CommandHandler("briefing", trigger_briefing))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("monitors", monitors_command))
    app.add_handler(CommandHandler("pulse", pulse_command))
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CommandHandler("version", show_version))
    app.add_handler(CallbackQueryHandler(_ask_user_callback, pattern=r"^ask_user:"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.PHOTO
                | filters.Document.ALL
                | filters.VOICE
                | filters.Sticker.STATIC
                | filters.LOCATION
                | filters.CONTACT
                | filters.AUDIO
                | filters.VIDEO
                | filters.VIDEO_NOTE
            )
            & ~filters.COMMAND,
            handle_message,
        )
    )

    app.post_init = notify_startup

    logger.info(
        "Teleclaude started — model: %s | github: %s | search: %s | tasks: %s | calendar: %s | email: %s",
        DEFAULT_MODEL,
        "on" if gh_client else "off",
        "on" if web_client else "off",
        "on" if tasks_client else "off",
        "on" if calendar_client else "off",
        "on" if email_client else "off",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
