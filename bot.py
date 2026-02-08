"""Teleclaude - Chat with Claude on Telegram. Code against GitHub."""

import asyncio
import datetime
import json
import logging
import os
import sys
import time

import anthropic
from dotenv import load_dotenv
from telegram import Update
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

SYSTEM_PROMPT = """You are Teleclaude, a coding assistant on Telegram with access to GitHub.

When the user sets a repo with /repo, you can read files, edit code, create branches, and open PRs.

Guidelines:
- Always read existing files before modifying them.
- Create a feature branch for changes — never commit directly to main.
- Write clear commit messages.
- When making multiple file changes, do them on the same branch, then open a single PR.
- Keep Telegram responses concise. Use code blocks for short snippets only.
- If no repo is set, you can still chat normally.
- Use web search to look up documentation, error messages, or current information when needed.
- Use Google Tasks to manage the user's tasks when they ask about todos, reminders, or task management.
- Use Google Calendar to check schedule, create events, or manage the user's calendar.
- You can send emails via Gmail but NEVER send an email without explicitly confirming the recipient, subject, and body with the user first."""

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
_tasks_tool_names = {t["name"] for t in TASKS_TOOLS}
_calendar_tool_names = {t["name"] for t in CALENDAR_TOOLS}
_email_tool_names = {t["name"] for t in EMAIL_TOOLS}

# In-memory cache (backed by SQLite)
conversations: dict[int, list] = {}
active_repos: dict[int, str] = {}
chat_models: dict[int, str] = {}


def get_model(chat_id: int) -> str:
    return chat_models.get(chat_id, DEFAULT_MODEL)


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


def trim_history(chat_id: int) -> None:
    history = get_conversation(chat_id)
    if len(history) > MAX_HISTORY * 2:
        conversations[chat_id] = history[-(MAX_HISTORY * 2) :]


def save_state(chat_id: int) -> None:
    """Persist current conversation to SQLite."""
    save_conversation(chat_id, get_conversation(chat_id))


async def send_long_message(update: Update, text: str) -> None:
    """Send a message, splitting if it exceeds Telegram's limit."""
    for i in range(0, len(text), MAX_TELEGRAM_LENGTH):
        await update.message.reply_text(text[i : i + MAX_TELEGRAM_LENGTH])


async def keep_typing(chat, stop_event: asyncio.Event, start_time: float, update: Update):
    """Keep the typing indicator alive and send progress messages."""
    progress_sent = False
    while not stop_event.is_set():
        await chat.send_action("typing")
        elapsed = time.time() - start_time
        if elapsed > PROGRESS_INTERVAL and not progress_sent:
            await update.message.reply_text("Still working on it...")
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
    clear_conversation(chat_id)
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


def _execute_tool_call(block, repo) -> str:
    """Dispatch a single tool call to the right handler."""
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text:
        return

    history = get_conversation(chat_id)
    history.append({"role": "user", "content": user_text})
    trim_history(chat_id)

    repo = get_active_repo(chat_id)
    tools = []
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

    system = SYSTEM_PROMPT
    if repo:
        system += f"\n\nActive repository: {repo}"

    # Start typing indicator in background
    stop_typing = asyncio.Event()
    start_time = time.time()
    typing_task = asyncio.create_task(
        keep_typing(update.effective_chat, stop_typing, start_time, update)
    )

    try:
        for round_num in range(MAX_TOOL_ROUNDS):
            kwargs = {
                "model": get_model(chat_id),
                "max_tokens": 4096,
                "system": system,
                "messages": history,
            }
            if tools:
                kwargs["tools"] = tools

            response = api_client.messages.create(**kwargs)

            if response.stop_reason != "tool_use":
                text_parts = [b.text for b in response.content if b.type == "text"]
                reply = "\n".join(text_parts) if text_parts else "(no response)"
                history.append({"role": "assistant", "content": response.content})
                save_state(chat_id)
                stop_typing.set()
                await typing_task
                await send_long_message(update, reply)
                return

            # Tool use round
            history.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Tool call [%d]: %s(%s)", round_num + 1, block.name, json.dumps(block.input)[:200])
                    result = _execute_tool_call(block, repo)
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
        await update.message.reply_text("(Reached tool call limit. Send another message to continue.)")

    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        history.pop()
        save_state(chat_id)
        stop_typing.set()
        await typing_task
        await update.message.reply_text(f"Claude API error: {e.message}")
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        history.pop()
        save_state(chat_id)
        stop_typing.set()
        await typing_task
        await update.message.reply_text("Something went wrong. Please try again.")


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
