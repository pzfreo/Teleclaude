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
import shutil
import sys
import time

import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from persistence import (
    init_db,
    load_conversation,
    save_conversation,
    clear_conversation,
    load_active_repo,
    save_active_repo,
    load_todos,
    save_todos,
    load_plan_mode,
    save_plan_mode,
    load_model,
    save_model,
    load_active_branch,
    save_active_branch,
)

load_dotenv()


class _RingBufferHandler(logging.Handler):
    """Keeps the last N log records in a deque for on-demand retrieval."""

    def __init__(self, capacity: int = 5000):
        super().__init__()
        self._buf: collections.deque[logging.LogRecord] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self._buf.append(record)

    def get_recent(self, seconds: int = 300) -> list[str]:
        """Return formatted log lines from the last `seconds` seconds."""
        cutoff = time.time() - seconds
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        return [formatter.format(r) for r in self._buf if r.created >= cutoff]


_ring_handler = _RingBufferHandler()

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
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

AVAILABLE_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-5-20250929",
    "haiku": "claude-haiku-4-5-20251001",
}

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set. Bot cannot start.")
    sys.exit(1)

if not ANTHROPIC_API_KEY:
    logger.error("ANTHROPIC_API_KEY is not set. Bot cannot start.")
    sys.exit(1)

# ── Optional integrations (each loads gracefully) ────────────────────

# GitHub
gh_client = None
GITHUB_TOOLS = []
execute_github_tool = None
try:
    from github_tools import GITHUB_TOOLS, GitHubClient, execute_tool as _execute_github
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
WEB_TOOLS = []
execute_web_tool = None
try:
    from web_tools import WEB_TOOLS, WebSearchClient, execute_tool as _execute_web
    web_client = WebSearchClient()
    execute_web_tool = _execute_web
    logger.info("Web search: enabled")
except Exception as e:
    logger.warning("Web search: failed to load (%s)", e)
    WEB_TOOLS = []

# Google Tasks
tasks_client = None
TASKS_TOOLS = []
execute_tasks_tool = None
try:
    from tasks_tools import TASKS_TOOLS, GoogleTasksClient, execute_tool as _execute_tasks
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
CALENDAR_TOOLS = []
execute_calendar_tool = None
try:
    from calendar_tools import CALENDAR_TOOLS, GoogleCalendarClient, execute_tool as _execute_calendar
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
EMAIL_TOOLS = []
execute_email_tool = None
try:
    from email_tools import EMAIL_TOOLS, GmailSendClient, execute_tool as _execute_email
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

# Claude Code CLI
claude_code_mgr = None
try:
    from claude_code import ClaudeCodeManager
    token = os.getenv("GITHUB_TOKEN", "")
    if token and shutil.which(os.getenv("CLAUDE_CLI_PATH", "") or "claude"):
        claude_code_mgr = ClaudeCodeManager(token)
        logger.info("Claude Code CLI: enabled (path=%s)", claude_code_mgr.cli_path)
    else:
        if not token:
            logger.info("Claude Code CLI: disabled (no GITHUB_TOKEN)")
        else:
            logger.info("Claude Code CLI: disabled (claude not found in PATH)")
except Exception as e:
    logger.warning("Claude Code CLI: failed to load (%s)", e)

# ── Bot config ───────────────────────────────────────────────────────

USER_TIMEZONE = os.getenv("TIMEZONE", "UTC")
DAILY_BRIEFING_TIME = os.getenv("DAILY_BRIEFING_TIME", "")  # e.g. "08:00"

SYSTEM_PROMPT = """You are Teleclaude, a personal AI assistant on Telegram. You help with coding, productivity, and daily tasks.

You have access to: GitHub (code, issues, PRs, CI), web search, Google Tasks, Google Calendar, and Gmail (send only).

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

Tool usage:
- Use web search to look up documentation, error messages, or current information.
- Use Google Tasks to manage the user's tasks when they ask about todos, reminders, or task management.
- Use Google Calendar to check schedule, create events, or manage the user's calendar.
- For time-specific events, use the user's timezone unless they specify otherwise.
- You can send emails via Gmail but NEVER send an email without explicitly confirming the recipient, subject, and body with the user first.
- For multi-step tasks, use the update_todo_list tool to track progress.
- When plan mode is on, always outline your plan first and wait for user approval before executing.
- You can upload binary files (images, etc.) to GitHub repos using the upload_binary_file tool.
- If a GitHub tool says "No active repo", tell the user to set one with /repo owner/name.

Attachments:
- Users can send photos, documents (images, PDFs, text files), stickers, locations, and contacts.
- You can see and analyze images. You can read PDFs and text files.
- If a user sends an image and wants it saved to a repo, use upload_binary_file with the base64 data from the image.
- Voice messages and video are not yet supported — ask the user to type instead.

You are a knowledgeable assistant across many domains — not just coding. You can help with writing, research, brainstorming, analysis, math, and general questions."""

# ── Internal tools (always available) ─────────────────────────────────

CLAUDE_CODE_TOOL = {
    "name": "run_claude_code",
    "description": (
        "Delegate a coding task to Claude Code, which has full filesystem access "
        "to the active repository clone — it can read, write, and create files, "
        "run bash commands, execute tests, and use git. Use this for any task "
        "that requires interacting with the codebase.\n\n"
        "Do NOT use this for non-coding tasks like calendar, email, tasks, or "
        "web search — use the dedicated tools for those.\n\n"
        "Context sharing: Include all relevant context from the conversation in "
        "your prompt — prior decisions, requirements discussed, error messages, "
        "architectural choices, etc. The CLI cannot see conversation history, "
        "so be thorough. If the user sent images or documents, include the "
        "file paths from the system context — the CLI can read them directly. "
        "The CLI maintains its own session, so follow-up coding tasks will have "
        "context from previous coding calls."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Complete task description with all relevant context",
            }
        },
        "required": ["prompt"],
    },
}

TODO_TOOL = {
    "name": "update_todo_list",
    "description": (
        "Update the task/todo list for the current session. Use this proactively to "
        "track progress on multi-step tasks. Each todo has a content string and a "
        "status: pending, in_progress, or completed. Send the full updated list each time."
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

# Build tool name sets for dispatch
_github_tool_names = {t["name"] for t in GITHUB_TOOLS}
_tasks_tool_names = {t["name"] for t in TASKS_TOOLS}
_calendar_tool_names = {t["name"] for t in CALENDAR_TOOLS}
_email_tool_names = {t["name"] for t in EMAIL_TOOLS}

# In-memory cache (backed by SQLite)
conversations: dict[int, list] = {}
active_repos: dict[int, str] = {}
active_branches: dict[int, str] = {}
chat_models: dict[int, str] = {}
chat_todos: dict[int, list[dict]] = {}
chat_plan_mode: dict[int, bool] = {}
# Per-chat locks to prevent concurrent message handling corruption
_chat_locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)


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
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


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


def _trim_content(content, keep_images: bool = True) -> any:
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

    # Strip thinking blocks from assistant content (they can't be replayed)
    for msg in history:
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            msg["content"] = [
                b for b in msg["content"]
                if not (isinstance(b, dict) and b.get("type") == "thinking")
            ]

    # Walk backwards and remove orphaned tool_use/tool_result pairs
    sanitized = []
    i = 0
    while i < len(history):
        msg = history[i]

        # Check if this assistant message has tool_use blocks
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            tool_use_ids = {
                b["id"] for b in msg["content"]
                if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b
            }

            if tool_use_ids:
                # There must be a next message with matching tool_results
                if i + 1 < len(history):
                    next_msg = history[i + 1]
                    if next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
                        result_ids = {
                            b.get("tool_use_id") for b in next_msg["content"]
                            if isinstance(b, dict) and b.get("type") == "tool_result"
                        }
                        if tool_use_ids <= result_ids:
                            # Pair is complete, keep both
                            sanitized.append(msg)
                            sanitized.append(next_msg)
                            i += 2
                            continue
                # Pair is broken — skip the assistant message (and orphaned results if any)
                logger.warning("Dropping orphaned tool_use message at index %d", i)
                i += 1
                continue

        sanitized.append(msg)
        i += 1

    # Ensure history starts with a user message
    while sanitized and sanitized[0].get("role") != "user":
        sanitized.pop(0)

    return sanitized


def trim_history(chat_id: int) -> None:
    history = get_conversation(chat_id)
    if len(history) > MAX_HISTORY * 2:
        conversations[chat_id] = history[-(MAX_HISTORY * 2) :]
    # Sanitize to fix any broken tool_use/tool_result pairs
    conversations[chat_id] = _sanitize_history(conversations.get(chat_id, []))
    msgs = conversations[chat_id]
    cutoff = max(0, len(msgs) - _KEEP_IMAGES_LAST_N)
    for i, msg in enumerate(msgs):
        msg["content"] = _trim_content(msg.get("content"), keep_images=(i >= cutoff))


def save_state(chat_id: int) -> None:
    """Persist current conversation to SQLite."""
    save_conversation(chat_id, get_conversation(chat_id))


async def send_long_message(chat_id: int, text: str, bot) -> None:
    """Send a message, splitting if it exceeds Telegram's limit."""
    if not text:
        return
    for i in range(0, len(text), MAX_TELEGRAM_LENGTH):
        try:
            await bot.send_message(chat_id=chat_id, text=text[i : i + MAX_TELEGRAM_LENGTH])
        except TelegramError as e:
            logger.warning("Failed to send message chunk to %d: %s", chat_id, e)


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
        except asyncio.TimeoutError:
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
        msg = f"Active repo set to: {repo} (default branch: {default_branch})"
        if claude_code_mgr and claude_code_mgr.available:
            msg += "\nCloning workspace in background..."
        await update.message.reply_text(msg)

        # Background clone for Claude Code
        if claude_code_mgr and claude_code_mgr.available:
            async def _clone_notify():
                try:
                    await claude_code_mgr.ensure_clone(repo)
                    await update.message.reply_text(f"Workspace ready: {repo}")
                except Exception as e:
                    logger.error("Background clone failed: %s", e)
                    await update.message.reply_text(f"Clone failed: {e}")
            asyncio.create_task(_clone_notify())
    except Exception as e:
        await update.message.reply_text(f"Can't access {repo}: {e}")


async def new_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    chat_todos[chat_id] = []
    chat_plan_mode[chat_id] = False
    chat_attachments.pop(chat_id, None)
    clear_conversation(chat_id)
    save_todos(chat_id, [])
    save_plan_mode(chat_id, False)
    set_active_branch(chat_id, None)
    if claude_code_mgr:
        claude_code_mgr.new_session(chat_id)
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
            await update.message.reply_text(
                f"{header}\n" + "\n".join(lines) + "\n\n/branch <number> or /branch <name>"
            )
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
                msg = f"Active branch set to: {branch}"
                if claude_code_mgr and claude_code_mgr.available:
                    ws = claude_code_mgr.workspace_path(repo)
                    if (ws / ".git").is_dir():
                        try:
                            await claude_code_mgr.checkout_branch(repo, branch)
                            msg += " (checked out locally)"
                        except Exception as e:
                            msg += f" (local checkout failed: {e})"
                await update.message.reply_text(msg)
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
    msg = f"Active branch set to: {branch_name}"

    # Checkout in local clone if available
    if claude_code_mgr and claude_code_mgr.available and repo:
        ws = claude_code_mgr.workspace_path(repo)
        if (ws / ".git").is_dir():
            try:
                await claude_code_mgr.checkout_branch(repo, branch_name)
                msg += " (checked out locally)"
            except Exception as e:
                msg += f" (local checkout failed: {e})"

    await update.message.reply_text(msg)


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
            return format_todo_list(todos)
        if block.name == "web_search" and execute_web_tool:
            return execute_web_tool(web_client, block.name, block.input)
        elif block.name in _tasks_tool_names and execute_tasks_tool:
            return execute_tasks_tool(tasks_client, block.name, block.input)
        elif block.name in _calendar_tool_names and execute_calendar_tool:
            return execute_calendar_tool(calendar_client, block.name, block.input)
        elif block.name in _email_tool_names and execute_email_tool:
            return execute_email_tool(email_client, block.name, block.input)
        elif block.name in _github_tool_names and execute_github_tool:
            if not repo:
                return "No active repo. Ask the user to set one with /repo owner/name first."
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
        return f"Tool error: {e}"


_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_SUPPORTED_DOC_MIMES = _IMAGE_MIME_TYPES | {"application/pdf"}

_MIME_TO_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "application/pdf": ".pdf",
}

# Track saved attachment paths per chat for Claude Code access
chat_attachments: dict[int, list[str]] = {}


def _save_attachment(chat_id: int, data: bytes, mime: str, label: str = "") -> str:
    """Save attachment to shared directory, return the absolute path."""
    if claude_code_mgr:
        shared_dir = claude_code_mgr.workspace_root / ".shared" / str(chat_id)
    else:
        shared_dir = _Path("workspaces/.shared") / str(chat_id)
    shared_dir.mkdir(parents=True, exist_ok=True)
    ext = _MIME_TO_EXT.get(mime, "")
    name = f"{label}_{int(time.time())}{ext}" if label else f"{int(time.time())}{ext}"
    path = shared_dir / name
    path.write_bytes(data)
    if chat_id not in chat_attachments:
        chat_attachments[chat_id] = []
    abs_path = str(path.resolve())
    chat_attachments[chat_id].append(abs_path)
    # Keep only last 20 attachments tracked
    chat_attachments[chat_id] = chat_attachments[chat_id][-20:]
    return abs_path


async def _download_telegram_file(file_obj, bot) -> bytes:
    """Download a Telegram file and return its bytes."""
    tg_file = await bot.get_file(file_obj.file_id)
    return bytes(await tg_file.download_as_bytearray())


async def _build_user_content(update: Update, bot) -> list[dict] | str | None:
    """Extract user content from a Telegram message: text, images, docs, voice, stickers, location.

    Returns a list of content blocks for multimodal, a plain string for text-only, or None if nothing useful.
    """
    msg = update.message
    content_blocks = []
    text = msg.text or msg.caption or ""

    chat_id = msg.chat_id

    # Photos (Telegram sends multiple sizes; take the largest)
    if msg.photo:
        try:
            photo = msg.photo[-1]  # highest resolution
            data = await _download_telegram_file(photo, bot)
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(data).decode()},
            })
            _save_attachment(chat_id, data, "image/jpeg", "photo")
        except Exception as e:
            logger.warning("Failed to download photo: %s", e)
            text += "\n[Photo attached but could not be downloaded]"

    # Stickers → treat as image
    if msg.sticker and not msg.sticker.is_animated and not msg.sticker.is_video:
        try:
            data = await _download_telegram_file(msg.sticker, bot)
            media_type = "image/webp"
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
            })
            _save_attachment(chat_id, data, media_type, "sticker")
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
                    content_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()},
                    })
                elif mime == "application/pdf":
                    content_blocks.append({
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()},
                    })
                _save_attachment(chat_id, data, mime, fname.rsplit(".", 1)[0])
            elif mime.startswith("text/") or fname.endswith((".txt", ".py", ".js", ".ts", ".json", ".md", ".csv", ".yaml", ".yml", ".toml", ".xml", ".html", ".css", ".sh", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".rb", ".sql", ".log")):
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
        text += "\n[Voice message received — voice transcription not supported yet. Please type your message instead.]"

    # Audio files
    if msg.audio:
        text += f"\n[Audio file: {msg.audio.title or msg.audio.file_name or 'audio'} — audio processing not supported]"

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

    # Build user content from text + any attachments
    user_content = await _build_user_content(update, context.bot)
    if not user_content:
        return

    lock = _chat_locks[chat_id]
    if lock.locked():
        try:
            await update.message.reply_text("Queued — I'll get to this once I finish the current request.")
        except TelegramError:
            pass
    async with lock:
        await _process_message(chat_id, user_content, update, context)


async def _execute_claude_code_tool(block, repo, chat_id, progress):
    """Execute a run_claude_code tool call via Claude Code CLI."""
    prompt = block.input.get("prompt", "")
    branch = get_active_branch(chat_id)
    model = get_model(chat_id)

    async def on_progress(tool_name):
        progress["tools"].append(f"cc:{tool_name}")

    try:
        result = await claude_code_mgr.run(
            chat_id=chat_id,
            repo=repo,
            prompt=prompt,
            branch=branch,
            model=model,
            on_progress=on_progress,
        )
    except Exception as e:
        logger.error("Claude Code run failed: %s", e, exc_info=True)
        result = f"Claude Code error: {e}"

    return result or "(no output)"


async def _process_message(chat_id: int, user_content, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Core message processing, runs under per-chat lock.

    user_content can be a plain string or a list of content blocks (multimodal).
    """
    bot = context.bot
    history = get_conversation(chat_id)
    history_len_before = len(history)
    history.append({"role": "user", "content": user_content})
    trim_history(chat_id)

    repo = get_active_repo(chat_id)
    tools = [TODO_TOOL]  # always available
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

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc
    now = datetime.datetime.now(tz)
    context_lines = [
        f"\n\nCurrent date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}",
        f"Timezone: {USER_TIMEZONE}",
        f"Model: {get_model(chat_id)}",
    ]
    system = SYSTEM_PROMPT + "\n".join(context_lines)
    if repo:
        branch = get_active_branch(chat_id)
        system += f"\n\nActive repository: {repo}"
        if branch:
            system += f"\nActive branch: {branch} — use this branch for file changes unless the user specifies otherwise."

    # Inject current todo list into context
    todos = get_todos(chat_id)
    if todos:
        system += f"\n\nCurrent todo list:\n{format_todo_list(todos)}"

    # Plan mode
    if get_plan_mode(chat_id):
        system += (
            "\n\nPLAN MODE IS ON. Before making any changes (file edits, PRs, emails, etc.), "
            "first outline a numbered plan of what you intend to do and ask the user to confirm. "
            "Only proceed after they approve. Use the update_todo_list tool to track the plan steps."
        )

    # Add Claude Code tool when repo is set and CLI is available
    if repo and claude_code_mgr and claude_code_mgr.available:
        tools.append(CLAUDE_CODE_TOOL)
        system += (
            "\n\nWhen run_claude_code is available, use it for any task involving the codebase — "
            "reading files, making changes, running tests, git operations. Include full context from "
            "the conversation in your prompt (the CLI can't see chat history or images). "
            "For non-coding tasks, use the dedicated tools."
        )
        # Tell Claude about saved attachments so it can pass paths to the CLI
        attachments = chat_attachments.get(chat_id, [])
        if attachments:
            paths = "\n".join(f"  - {p}" for p in attachments[-5:])
            system += (
                f"\n\nRecent attachments saved to disk (the CLI can read these directly):\n{paths}\n"
                "When delegating tasks that reference these files, include the file paths in your prompt."
            )

    max_rounds = MAX_TOOL_ROUNDS

    # Shared progress status — the tool loop writes, keep_typing reads
    progress = {"round": 0, "max": max_rounds, "tools": [], "last_update_round": -1}

    # Start typing indicator in background
    stop_typing = asyncio.Event()
    start_time = time.time()
    typing_task = asyncio.create_task(
        keep_typing(update.effective_chat, stop_typing, start_time, bot, progress)
    )

    try:
        for round_num in range(max_rounds):
            progress["round"] = round_num + 1

            kwargs = {
                "model": get_model(chat_id),
                "max_tokens": 8192,
                "system": system,
                "messages": history,
            }
            if tools:
                kwargs["tools"] = tools

            response = await _call_anthropic(**kwargs)

            if response.stop_reason != "tool_use":
                text_parts = [b.text for b in response.content if b.type == "text"]
                reply = "\n".join(text_parts) if text_parts else "(no response)"
                history.append({"role": "assistant", "content": response.content})
                save_state(chat_id)
                stop_typing.set()
                await typing_task
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
                    if block.name == "run_claude_code":
                        result = await _execute_claude_code_tool(block, repo, chat_id, progress)
                    else:
                        result = await loop.run_in_executor(
                            None, _execute_tool_call, block, repo, chat_id
                        )
                    # Claude Code results get higher limit to preserve detail
                    max_result = 50000 if block.name == "run_claude_code" else 10000
                    if len(result) > max_result:
                        result = result[:max_result] + "\n... (truncated)"
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    )

            history.append({"role": "user", "content": tool_results})
            # Save after each tool round in case of crash
            save_state(chat_id)

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


async def generate_briefing(bot, chat_id: int) -> None:
    """Generate and send a daily briefing using Claude with available tools."""
    tools = []
    if tasks_client:
        tools.extend(TASKS_TOOLS)
    if calendar_client:
        tools.extend(CALENDAR_TOOLS)

    if not tools:
        await bot.send_message(chat_id=chat_id, text="No calendar or tasks configured — nothing to brief on.")
        return

    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(USER_TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc
    now = datetime.datetime.now(tz)

    briefing_prompt = (
        f"Today is {now.strftime('%A, %B %d, %Y')} ({USER_TIMEZONE}).\n\n"
        "Give me a concise morning briefing. Check my calendar for today's events "
        "and my task list for pending items. Format it as:\n"
        "- A quick summary line (e.g. '3 events, 5 tasks')\n"
        "- Today's schedule in chronological order\n"
        "- Top pending tasks\n"
        "Keep it short and scannable for a phone screen."
    )

    messages = [{"role": "user", "content": briefing_prompt}]

    for _ in range(10):
        response = await _call_anthropic(
            model=DEFAULT_MODEL,
            max_tokens=2048,
            system="You are Teleclaude generating a daily briefing. Be concise and useful.",
            messages=messages,
            tools=tools,
        )

        if response.stop_reason != "tool_use":
            text_parts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_parts) if text_parts else "No briefing available."
            await send_long_message(chat_id, reply, bot)
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    if block.name in _tasks_tool_names and execute_tasks_tool:
                        result = execute_tasks_tool(tasks_client, block.name, block.input)
                    elif block.name in _calendar_tool_names and execute_calendar_tool:
                        result = execute_calendar_tool(calendar_client, block.name, block.input)
                    else:
                        result = f"Tool '{block.name}' not available for briefing."
                except Exception as e:
                    result = f"Tool error: {e}"
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    await bot.send_message(chat_id=chat_id, text="Briefing generation hit tool limit.")


async def scheduled_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback for the daily scheduled briefing."""
    for user_id in ALLOWED_USER_IDS:
        try:
            await generate_briefing(context.bot, user_id)
        except Exception as e:
            logger.warning("Failed to send briefing to %d: %s", user_id, e)


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


async def notify_startup(app: Application) -> None:
    """Send a startup message to all allowed users."""
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
    if claude_code_mgr and claude_code_mgr.available:
        integrations.append("Claude Code CLI")

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CommandHandler("version", show_version))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE
         | filters.Sticker.STATIC | filters.LOCATION | filters.CONTACT
         | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE)
        & ~filters.COMMAND,
        handle_message,
    ))

    # Schedule daily briefing
    if DAILY_BRIEFING_TIME and ALLOWED_USER_IDS:
        if app.job_queue is None:
            logger.warning("JobQueue not available — install python-telegram-bot[job-queue] for daily briefings")
        else:
            try:
                import zoneinfo
                tz = zoneinfo.ZoneInfo(USER_TIMEZONE)
            except Exception:
                tz = datetime.timezone.utc
            hour, minute = (int(x) for x in DAILY_BRIEFING_TIME.split(":"))
            briefing_time = datetime.time(hour=hour, minute=minute, tzinfo=tz)
            app.job_queue.run_daily(scheduled_briefing, time=briefing_time)
            logger.info("Daily briefing scheduled at %s %s", DAILY_BRIEFING_TIME, USER_TIMEZONE)
    elif DAILY_BRIEFING_TIME:
        logger.info("Daily briefing configured but no ALLOWED_USER_IDS set — skipping")

    app.post_init = notify_startup

    logger.info(
        "Teleclaude started — model: %s | github: %s | search: %s | tasks: %s | calendar: %s | email: %s | claude-code: %s",
        DEFAULT_MODEL,
        "on" if gh_client else "off",
        "on" if web_client else "off",
        "on" if tasks_client else "off",
        "on" if calendar_client else "off",
        "on" if email_client else "off",
        "on" if (claude_code_mgr and claude_code_mgr.available) else "off",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
