"""Shared utilities used by both bot.py and bot_agent.py."""

import collections
import functools
import logging
import time

from telegram.error import TelegramError

logger = logging.getLogger(__name__)


# ── Logging ──────────────────────────────────────────────────────────


class RingBufferHandler(logging.Handler):
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


# ── Auth ─────────────────────────────────────────────────────────────


def is_authorized(user_id: int, allowed_ids: set[int]) -> bool:
    """Check if a user ID is in the allowlist. Empty allowlist permits all."""
    if not allowed_ids:
        return True
    return user_id in allowed_ids


def require_auth(allowed_ids: set[int]):
    """Decorator factory that checks authorization before running a handler.

    Usage:
        @require_auth(ALLOWED_USER_IDS)
        async def my_handler(update, context): ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context):
            if not is_authorized(update.effective_user.id, allowed_ids):
                try:
                    await update.message.reply_text("Sorry, you're not authorized to use this bot.")
                except TelegramError:
                    pass
                return
            return await func(update, context)

        return wrapper

    return decorator


# ── Telegram helpers ─────────────────────────────────────────────────

MAX_TELEGRAM_LENGTH = 4096


async def send_long_message(chat_id: int, text: str, bot) -> None:
    """Send a message, splitting if it exceeds Telegram's limit."""
    if not text:
        return
    for i in range(0, len(text), MAX_TELEGRAM_LENGTH):
        try:
            await bot.send_message(chat_id=chat_id, text=text[i : i + MAX_TELEGRAM_LENGTH])
        except TelegramError as e:
            logger.warning("Failed to send message chunk to %d: %s", chat_id, e)


async def download_telegram_file(file_obj, bot) -> bytes:
    """Download a Telegram file and return its bytes."""
    tg_file = await bot.get_file(file_obj.file_id)
    return bytes(await tg_file.download_as_bytearray())
