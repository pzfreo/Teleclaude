"""Streaming response handler for Teleclaude.

Accumulates text from Anthropic's streaming API and progressively updates
a Telegram message via edit_message_text() at a throttled rate.
"""

import logging
import time

from telegram.error import BadRequest, TelegramError

logger = logging.getLogger(__name__)

# Minimum interval between Telegram message edits (seconds)
EDIT_THROTTLE_INTERVAL = 1.0
# Buffer zone before the 4096 limit to trigger a new message
SPLIT_THRESHOLD = 3900


def _close_unclosed_code_blocks(text: str) -> str:
    """If the text has an odd number of ``` markers, append a closing one.

    Prevents broken markdown rendering during progressive streaming updates.
    """
    count = text.count("```")
    if count % 2 == 1:
        return text + "\n```"
    return text


class StreamingResponder:
    """Accumulates streaming text and progressively updates a Telegram message.

    Usage::

        responder = StreamingResponder(bot, chat_id)
        async for text_chunk in stream.text_stream:
            await responder.feed(text_chunk)
        await responder.finalize()
    """

    def __init__(self, bot, chat_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._accumulated = ""
        self._committed_offset = 0  # chars committed to finalized (split) messages
        self._current_msg_id: int | None = None
        self._last_edit_time = 0.0
        self._dirty = False
        self._failed = False
        self._finalized = False

    @property
    def full_text(self) -> str:
        """Return the full accumulated text."""
        return self._accumulated

    async def feed(self, chunk: str) -> None:
        """Feed a text chunk from the stream. Triggers throttled Telegram edits."""
        self._accumulated += chunk
        self._dirty = True

        now = time.monotonic()
        elapsed = now - self._last_edit_time

        if elapsed >= EDIT_THROTTLE_INTERVAL:
            await self._flush()

    async def finalize(self) -> None:
        """Flush any remaining text after the stream completes."""
        if self._finalized:
            return
        self._finalized = True

        if self._dirty or not self._current_msg_id:
            await self._flush()

    async def _flush(self) -> None:
        """Push accumulated text to Telegram (send or edit)."""
        self._dirty = False
        self._last_edit_time = time.monotonic()

        current_text = self._accumulated[self._committed_offset :]
        if not current_text.strip():
            return

        if self._failed:
            return

        # Need to split into a new message?
        if len(current_text) > SPLIT_THRESHOLD and self._current_msg_id is not None:
            split_text = current_text[:SPLIT_THRESHOLD]
            split_text = _close_unclosed_code_blocks(split_text)
            await self._edit_message(split_text)
            self._committed_offset += SPLIT_THRESHOLD
            current_text = self._accumulated[self._committed_offset :]
            self._current_msg_id = None

        if not current_text.strip():
            return

        display_text = _close_unclosed_code_blocks(current_text)

        if self._current_msg_id is None:
            await self._send_new_message(display_text)
        else:
            await self._edit_message(display_text)

    async def _send_new_message(self, text: str) -> None:
        """Send a new Telegram message and track its ID."""
        try:
            msg = await self._bot.send_message(chat_id=self._chat_id, text=text)
            self._current_msg_id = msg.message_id
        except TelegramError as e:
            logger.warning("Failed to send streaming message to %d: %s", self._chat_id, e)
            self._failed = True

    async def _edit_message(self, text: str) -> None:
        """Edit the current Telegram message. Falls back on failure."""
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._current_msg_id,
                text=text,
            )
        except BadRequest as e:
            error_msg = str(e).lower()
            if "message is not modified" in error_msg:
                pass  # text hasn't changed enough — not an error
            elif "message to edit not found" in error_msg:
                logger.warning("Message %d not found for edit, falling back", self._current_msg_id)
                self._failed = True
            else:
                logger.warning("Failed to edit message %d: %s", self._current_msg_id, e)
                self._failed = True
        except TelegramError as e:
            logger.warning("Telegram error editing message %d: %s", self._current_msg_id, e)
            self._failed = True
