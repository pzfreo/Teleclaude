"""Teleclaude - Chat with Claude on Telegram. Code against GitHub."""

import asyncio
import collections
import datetime
import json
import logging
import os
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
    load_agent_mode,
    save_agent_mode,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Required config ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

AVAILABLE_MODELS = {
    "opus": "claude-opus-4-20250514",
    "sonnet": "claude-sonnet-4-20250514",
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

You are a knowledgeable assistant across many domains — not just coding. You can help with writing, research, brainstorming, analysis, math, and general questions."""

# ── Internal tools (always available) ─────────────────────────────────

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
MAX_TOOL_ROUNDS_AGENT = 30
TYPING_INTERVAL = 4  # seconds between typing indicator refreshes
PROGRESS_INTERVAL = 15  # seconds before sending a progress message

api_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Build tool name sets for dispatch
_tasks_tool_names = {t["name"] for t in TASKS_TOOLS}
_calendar_tool_names = {t["name"] for t in CALENDAR_TOOLS}
_email_tool_names = {t["name"] for t in EMAIL_TOOLS}

# In-memory cache (backed by SQLite)
conversations: dict[int, list] = {}
active_repos: dict[int, str] = {}
chat_models: dict[int, str] = {}
chat_todos: dict[int, list[dict]] = {}
chat_plan_mode: dict[int, bool] = {}
chat_agent_mode: dict[int, bool] = {}

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
    return chat_models.get(chat_id, DEFAULT_MODEL)


def get_todos(chat_id: int) -> list[dict]:
    if chat_id not in chat_todos:
        chat_todos[chat_id] = load_todos(chat_id)
    return chat_todos[chat_id]


def get_plan_mode(chat_id: int) -> bool:
    if chat_id not in chat_plan_mode:
        chat_plan_mode[chat_id] = load_plan_mode(chat_id)
    return chat_plan_mode[chat_id]


def get_agent_mode(chat_id: int) -> bool:
    if chat_id not in chat_agent_mode:
        chat_agent_mode[chat_id] = load_agent_mode(chat_id)
    return chat_agent_mode[chat_id]


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
    """Get conversation from cache or load from DB."""
    if chat_id not in conversations:
        conversations[chat_id] = load_conversation(chat_id)
    return conversations[chat_id]


def get_active_repo(chat_id: int) -> str | None:
    """Get active repo from cache or load from DB."""
    if chat_id not in active_repos:
        repo = load_active_repo(chat_id)
        if repo:
            active_repos[chat_id] = repo
    return active_repos.get(chat_id)


MAX_CONTENT_SIZE = 20000  # max chars per content string in history


def _trim_content(content) -> any:
    """Truncate oversized content blocks when reloading history."""
    if isinstance(content, str) and len(content) > MAX_CONTENT_SIZE:
        return content[:MAX_CONTENT_SIZE] + "\n... (truncated)"
    if isinstance(content, list):
        trimmed = []
        for item in content:
            if isinstance(item, dict):
                item = dict(item)  # shallow copy
                if isinstance(item.get("content"), str) and len(item["content"]) > MAX_CONTENT_SIZE:
                    item["content"] = item["content"][:MAX_CONTENT_SIZE] + "\n... (truncated)"
            trimmed.append(item)
        return trimmed
    return content


def trim_history(chat_id: int) -> None:
    history = get_conversation(chat_id)
    if len(history) > MAX_HISTORY * 2:
        conversations[chat_id] = history[-(MAX_HISTORY * 2) :]
    # Trim any oversized content blocks to prevent context bloat
    for msg in conversations.get(chat_id, []):
        msg["content"] = _trim_content(msg.get("content"))


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


async def keep_typing(chat, stop_event: asyncio.Event, start_time: float, bot):
    """Keep the typing indicator alive and send progress messages."""
    progress_sent = False
    while not stop_event.is_set():
        try:
            await chat.send_action("typing")
        except TelegramError:
            pass  # chat may have been deleted or bot blocked
        elapsed = time.time() - start_time
        if elapsed > PROGRESS_INTERVAL and not progress_sent:
            try:
                await bot.send_message(chat_id=chat.id, text="Still working on it...")
            except TelegramError:
                pass
            progress_sent = True
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
        "/repo - Show current repo\n"
        "/new - Start a fresh conversation\n"
        "/model - Show or switch Claude model (opus/sonnet/haiku)\n"
        "/plan - Toggle plan mode (outline before executing)\n"
        "/agent - Toggle agent mode (autonomous + extended thinking)\n"
        "/todo - Show current task list (/todo clear to reset)\n"
        "/briefing - Get a daily summary of calendar + tasks\n"
        "/help - Show this message\n\n"
        f"{status}"
    )


async def set_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        repo = get_active_repo(chat_id)
        if repo:
            await update.message.reply_text(f"Active repo: {repo}")
        else:
            await update.message.reply_text("No repo set. Use: /repo owner/name")
        return

    repo = context.args[0]
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
        await update.message.reply_text(f"Active repo set to: {repo} (default branch: {default_branch})")
    except Exception as e:
        await update.message.reply_text(f"Can't access {repo}: {e}")


async def new_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    chat_todos[chat_id] = []
    clear_conversation(chat_id)
    save_todos(chat_id, [])
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


async def toggle_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    current = get_agent_mode(chat_id)
    new_mode = not current
    chat_agent_mode[chat_id] = new_mode
    save_agent_mode(chat_id, new_mode)
    if new_mode:
        await update.message.reply_text(
            "Agent mode ON. I'll work autonomously:\n"
            "- Extended thinking for deeper reasoning\n"
            "- Up to 30 tool rounds per message\n"
            "- Full repo exploration before changes\n"
            "- CI/test checks after changes\n"
            "- Proactive issue investigation"
        )
    else:
        await update.message.reply_text("Agent mode OFF. Back to normal conversational mode.")


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
        elif execute_github_tool:
            return execute_github_tool(gh_client, repo, block.name, block.input)
        return f"Tool '{block.name}' is not available."
    except Exception as e:
        logger.error("Tool '%s' crashed: %s", block.name, e, exc_info=True)
        return f"Tool error: {e}"


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
    user_text = update.message.text
    if not user_text:
        return

    async with _chat_locks[chat_id]:
        await _process_message(chat_id, user_text, update, context)


async def _process_message(chat_id: int, user_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Core message processing, runs under per-chat lock."""
    bot = context.bot
    history = get_conversation(chat_id)
    history_len_before = len(history)
    history.append({"role": "user", "content": user_text})
    trim_history(chat_id)

    repo = get_active_repo(chat_id)
    tools = [TODO_TOOL]  # always available
    if repo and gh_client:
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
        system += f"\n\nActive repository: {repo}"

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

    agent_mode = get_agent_mode(chat_id)
    if agent_mode:
        system += (
            "\n\nAGENT MODE IS ON. You are an autonomous coding agent. Work like Claude Code:"
            "\n- Think deeply about the problem before acting."
            "\n- For coding tasks: explore the repo structure first (use get_tree), read relevant files, "
            "understand the codebase, then make changes."
            "\n- Always use update_todo_list to track your progress on multi-step tasks."
            "\n- After making code changes, check if the repo has CI workflows and review their status."
            "\n- If CI fails, read the logs, diagnose, and fix the issue."
            "\n- Be thorough — investigate related files, not just the one that was mentioned."
            "\n- When fixing bugs: find the root cause, don't just patch symptoms."
            "\n- When adding features: consider edge cases, error handling, and test coverage."
            "\n- Report back concisely when done with a summary of what you did."
        )

    max_rounds = MAX_TOOL_ROUNDS_AGENT if agent_mode else MAX_TOOL_ROUNDS

    # Start typing indicator in background
    stop_typing = asyncio.Event()
    start_time = time.time()
    typing_task = asyncio.create_task(
        keep_typing(update.effective_chat, stop_typing, start_time, bot)
    )

    try:
        for round_num in range(max_rounds):
            kwargs = {
                "model": get_model(chat_id),
                "max_tokens": 16384 if agent_mode else 4096,
                "system": system,
                "messages": history,
            }
            if tools:
                kwargs["tools"] = tools

            # Extended thinking in agent mode
            if agent_mode:
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10000}

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
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Tool call [%d]: %s(%s)", round_num + 1, block.name, json.dumps(block.input)[:200])
                    result = _execute_tool_call(block, repo, chat_id)
                    if len(result) > 10000:
                        result = result[:10000] + "\n... (truncated)"
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

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    enabled = ", ".join(integrations) if integrations else "none"
    msg = f"Teleclaude restarted at {now}\nModel: {DEFAULT_MODEL}\nIntegrations: {enabled}"

    for user_id in ALLOWED_USER_IDS:
        try:
            await app.bot.send_message(chat_id=user_id, text=msg)
            logger.info("Sent startup notification to user %d", user_id)
        except Exception as e:
            logger.warning("Could not notify user %d: %s", user_id, e)


def main() -> None:
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("repo", set_repo))
    app.add_handler(CommandHandler("new", new_conversation))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(CommandHandler("plan", toggle_plan))
    app.add_handler(CommandHandler("agent", toggle_agent))
    app.add_handler(CommandHandler("todo", show_todos))
    app.add_handler(CommandHandler("briefing", trigger_briefing))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
