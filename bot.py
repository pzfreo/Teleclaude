"""Teleclaude - Chat with Claude on Telegram. Code against GitHub."""

import json
import logging
import os
import sys
from collections import defaultdict

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

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Required config ──────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

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
        logger.info("Google Tasks: disabled (missing GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, or GOOGLE_REFRESH_TOKEN)")
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

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Build tool name sets for dispatch
_tasks_tool_names = {t["name"] for t in TASKS_TOOLS}
_calendar_tool_names = {t["name"] for t in CALENDAR_TOOLS}
_email_tool_names = {t["name"] for t in EMAIL_TOOLS}

# Per-chat state
conversations: dict[int, list[dict]] = defaultdict(list)
active_repos: dict[int, str] = {}  # chat_id -> "owner/repo"


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def trim_history(chat_id: int) -> None:
    history = conversations[chat_id]
    if len(history) > MAX_HISTORY * 2:
        conversations[chat_id] = history[-(MAX_HISTORY * 2) :]


async def send_long_message(update: Update, text: str) -> None:
    """Send a message, splitting if it exceeds Telegram's limit."""
    for i in range(0, len(text), MAX_TELEGRAM_LENGTH):
        await update.message.reply_text(text[i : i + MAX_TELEGRAM_LENGTH])


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
        "/model - Show current Claude model\n"
        "/help - Show this message\n\n"
        f"{status}"
    )


async def set_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        repo = active_repos.get(chat_id)
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

    # Verify the repo is accessible
    try:
        default_branch = gh_client.get_default_branch(repo)
        active_repos[chat_id] = repo
        await update.message.reply_text(f"Active repo set to: {repo} (default branch: {default_branch})")
    except Exception as e:
        await update.message.reply_text(f"Can't access {repo}: {e}")


async def new_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    await update.message.reply_text("Conversation cleared. Starting fresh.")


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(f"Current model: {CLAUDE_MODEL}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text
    if not user_text:
        return

    conversations[chat_id].append({"role": "user", "content": user_text})
    trim_history(chat_id)

    repo = active_repos.get(chat_id)
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

    await update.effective_chat.send_action("typing")

    try:
        # Tool-use loop: Claude may call tools multiple times
        for _ in range(MAX_TOOL_ROUNDS):
            kwargs = {
                "model": CLAUDE_MODEL,
                "max_tokens": 4096,
                "system": system,
                "messages": conversations[chat_id],
            }
            if tools:
                kwargs["tools"] = tools

            response = client.messages.create(**kwargs)

            # If Claude doesn't want to use tools, we're done
            if response.stop_reason != "tool_use":
                # Extract text from response
                text_parts = [b.text for b in response.content if b.type == "text"]
                reply = "\n".join(text_parts) if text_parts else "(no response)"
                conversations[chat_id].append({"role": "assistant", "content": response.content})
                await send_long_message(update, reply)
                return

            # Claude wants to use tools — execute them
            conversations[chat_id].append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logger.info("Tool call: %s(%s)", block.name, json.dumps(block.input)[:200])
                    if block.name == "web_search" and execute_web_tool:
                        result = execute_web_tool(web_client, block.name, block.input)
                    elif block.name in _tasks_tool_names and execute_tasks_tool:
                        result = execute_tasks_tool(tasks_client, block.name, block.input)
                    elif block.name in _calendar_tool_names and execute_calendar_tool:
                        result = execute_calendar_tool(calendar_client, block.name, block.input)
                    elif block.name in _email_tool_names and execute_email_tool:
                        result = execute_email_tool(email_client, block.name, block.input)
                    elif execute_github_tool:
                        result = execute_github_tool(gh_client, repo, block.name, block.input)
                    else:
                        result = f"Tool '{block.name}' is not available."
                    # Truncate large results to avoid blowing up context
                    if len(result) > 10000:
                        result = result[:10000] + "\n... (truncated)"
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result}
                    )

            conversations[chat_id].append({"role": "user", "content": tool_results})
            await update.effective_chat.send_action("typing")

        await update.message.reply_text("(Reached tool call limit. Send another message to continue.)")

    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        conversations[chat_id].pop()
        await update.message.reply_text(f"Claude API error: {e.message}")
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        conversations[chat_id].pop()
        await update.message.reply_text("Something went wrong. Please try again.")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("repo", set_repo))
    app.add_handler(CommandHandler("new", new_conversation))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(
        "Teleclaude started — model: %s | github: %s | search: %s | tasks: %s | calendar: %s | email: %s",
        CLAUDE_MODEL,
        "on" if gh_client else "off",
        "on" if web_client else "off",
        "on" if tasks_client else "off",
        "on" if calendar_client else "off",
        "on" if email_client else "off",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
