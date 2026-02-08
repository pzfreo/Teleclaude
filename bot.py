"""Teleclaude - Chat with Claude on Telegram. Code against GitHub."""

import json
import logging
import os
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

from github_tools import GITHUB_TOOLS, GitHubClient, execute_tool as execute_github_tool
from web_tools import WEB_TOOLS, WebSearchClient, execute_tool as execute_web_tool
from tasks_tools import TASKS_TOOLS, GoogleTasksClient, execute_tool as execute_tasks_tool

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

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
- Use Google Tasks to manage the user's tasks when they ask about todos, reminders, or task management."""

ALLOWED_USER_IDS: set[int] = set()
for uid in os.getenv("ALLOWED_USER_IDS", "").split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USER_IDS.add(int(uid))

MAX_HISTORY = 50
MAX_TELEGRAM_LENGTH = 4096
MAX_TOOL_ROUNDS = 15

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gh_client = GitHubClient(GITHUB_TOKEN) if GITHUB_TOKEN else None
web_client = WebSearchClient()
tasks_client = (
    GoogleTasksClient(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN)
    if GOOGLE_REFRESH_TOKEN
    else None
)

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

    github_status = "connected" if gh_client else "not configured (set GITHUB_TOKEN)"
    tasks_status = "connected" if tasks_client else "not configured (run setup_google.py)"
    await update.message.reply_text(
        "Hello! I'm Teleclaude — Claude on Telegram with GitHub, web search, and Google Tasks.\n\n"
        "Commands:\n"
        "/repo owner/name - Set the active GitHub repo\n"
        "/repo - Show current repo\n"
        "/new - Start a fresh conversation\n"
        "/model - Show current Claude model\n"
        "/help - Show this message\n\n"
        f"GitHub: {github_status}\n"
        f"Google Tasks: {tasks_status}\n"
        "Web search: enabled"
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
    tools.extend(WEB_TOOLS)
    if tasks_client:
        tools.extend(TASKS_TOOLS)

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
                    if block.name == "web_search":
                        result = execute_web_tool(web_client, block.name, block.input)
                    elif block.name in {t["name"] for t in TASKS_TOOLS}:
                        result = execute_tasks_tool(tasks_client, block.name, block.input)
                    else:
                        result = execute_github_tool(gh_client, repo, block.name, block.input)
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

    logger.info("Teleclaude bot started (model: %s, github: %s, search: %s, tasks: %s)", CLAUDE_MODEL, bool(gh_client), bool(web_client), bool(tasks_client))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
