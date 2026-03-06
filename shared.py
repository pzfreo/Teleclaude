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
    """Send a message, splitting at line boundaries to respect Telegram's 4096-char limit."""
    if not text:
        return
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > MAX_TELEGRAM_LENGTH:
            if current_parts:
                chunks.append("".join(current_parts))
                current_parts = []
                current_len = 0
            # Single line longer than the limit — hard split it
            while len(line) > MAX_TELEGRAM_LENGTH:
                chunks.append(line[:MAX_TELEGRAM_LENGTH])
                line = line[MAX_TELEGRAM_LENGTH:]
        current_parts.append(line)
        current_len += len(line)
    if current_parts:
        chunks.append("".join(current_parts))
    for chunk in chunks:
        try:
            await bot.send_message(chat_id=chat_id, text=chunk)
        except TelegramError as e:
            logger.warning("Failed to send message chunk to %d: %s", chat_id, e)


async def download_telegram_file(file_obj, bot) -> bytes:
    """Download a Telegram file and return its bytes."""
    tg_file = await bot.get_file(file_obj.file_id)
    return bytes(await tg_file.download_as_bytearray())
