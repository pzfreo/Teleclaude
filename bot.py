"""Teleclaude - A Telegram bot that connects you to Claude."""

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

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful assistant responding via Telegram. Keep responses concise but thorough.",
)
ALLOWED_USER_IDS: set[int] = set()
for uid in os.getenv("ALLOWED_USER_IDS", "").split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USER_IDS.add(int(uid))

MAX_HISTORY = 50  # max message pairs to keep per conversation
MAX_TELEGRAM_LENGTH = 4096

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# conversation history keyed by chat_id
conversations: dict[int, list[dict]] = defaultdict(list)


def is_authorized(user_id: int) -> bool:
    """Check if a user is authorized to use the bot."""
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def trim_history(chat_id: int) -> None:
    """Keep conversation history within limits."""
    history = conversations[chat_id]
    if len(history) > MAX_HISTORY * 2:
        conversations[chat_id] = history[-(MAX_HISTORY * 2) :]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    await update.message.reply_text(
        "Hello! I'm Teleclaude — a bridge to Claude.\n\n"
        "Just send me a message and I'll forward it to Claude and relay the response.\n\n"
        "Commands:\n"
        "/new - Start a fresh conversation\n"
        "/model - Show current Claude model\n"
        "/help - Show this message"
    )


async def new_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command - clear conversation history."""
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    conversations[chat_id] = []
    await update.message.reply_text("Conversation cleared. Starting fresh.")


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command."""
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(f"Current model: {CLAUDE_MODEL}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages by sending them to Claude."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    if not user_text:
        return

    # Add user message to history
    conversations[chat_id].append({"role": "user", "content": user_text})
    trim_history(chat_id)

    # Show typing indicator
    await update.effective_chat.send_action("typing")

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=conversations[chat_id],
        )
        reply = response.content[0].text
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        # Remove the failed user message from history
        conversations[chat_id].pop()
        await update.message.reply_text(f"Claude API error: {e.message}")
        return
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        conversations[chat_id].pop()
        await update.message.reply_text("Something went wrong. Please try again.")
        return

    # Add assistant response to history
    conversations[chat_id].append({"role": "assistant", "content": reply})

    # Split long messages to stay within Telegram's limit
    for i in range(0, len(reply), MAX_TELEGRAM_LENGTH):
        chunk = reply[i : i + MAX_TELEGRAM_LENGTH]
        await update.message.reply_text(chunk)


def main() -> None:
    """Start the bot."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("new", new_conversation))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Teleclaude bot started (model: %s)", CLAUDE_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
