"""Shared test helpers for Teleclaude tests."""

from unittest.mock import AsyncMock, MagicMock


def make_update(chat_id=1001, user_id=42, text="hello", message=None):
    """Create a minimal mock Update for handler tests."""
    update = MagicMock()
    update.effective_chat = AsyncMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    if message is None:
        msg = MagicMock()
        msg.text = text
        msg.caption = None
        msg.photo = None
        msg.sticker = None
        msg.document = None
        msg.voice = None
        msg.audio = None
        msg.video = None
        msg.video_note = None
        msg.location = None
        msg.contact = None
        msg.reply_text = AsyncMock()
        update.message = msg
    else:
        update.message = message
    return update


def make_context(bot=None, args=None, job_queue=None):
    """Create a minimal mock ContextTypes.DEFAULT_TYPE."""
    ctx = MagicMock()
    ctx.bot = bot or AsyncMock()
    ctx.args = args or []
    ctx.application.job_queue = job_queue
    return ctx
