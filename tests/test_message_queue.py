"""Tests for message queuing and ! interrupt in bot.py."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCancelEvents:
    """Tests for the cancel event mechanism."""

    def test_cancel_events_dict_exists(self):
        from bot import _cancel_events

        assert isinstance(_cancel_events, dict)

    @pytest.mark.asyncio
    async def test_bang_sets_cancel_event(self):
        """Sending '!' should set the cancel event for that chat."""
        from bot import _cancel_events, handle_message

        chat_id = 12345
        cancel_event = asyncio.Event()
        _cancel_events[chat_id] = cancel_event

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "!"
        update.message.reply_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id

        context = MagicMock()

        with patch("bot.is_authorized", return_value=True):
            await handle_message(update, context)

        assert cancel_event.is_set()
        update.message.reply_text.assert_called_once_with("Cancelling...")

        # Clean up
        _cancel_events.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_bang_with_no_active_request(self):
        """Sending '!' with nothing running should say 'Nothing to cancel'."""
        from bot import _cancel_events, handle_message

        chat_id = 99999

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "!"
        update.message.reply_text = AsyncMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id

        context = MagicMock()

        # Ensure no cancel event exists
        _cancel_events.pop(chat_id, None)

        with patch("bot.is_authorized", return_value=True):
            await handle_message(update, context)

        update.message.reply_text.assert_called_once_with("Nothing to cancel.")

    @pytest.mark.asyncio
    async def test_cancel_rolls_back_history(self, tmp_db):
        """When cancel is set, _process_message should roll back history."""
        from bot import _process_message, conversations

        chat_id = 77777
        conversations[chat_id] = [
            {"role": "user", "content": "previous message"},
            {"role": "assistant", "content": "previous response"},
        ]
        history_before = len(conversations[chat_id])

        cancel = asyncio.Event()
        cancel.set()  # Pre-set = immediate cancel

        update = MagicMock()
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        context = MagicMock()
        context.bot = AsyncMock()
        context.bot.send_message = AsyncMock()

        with (
            patch("bot.save_state"),
            patch("bot.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot.trim_history"),
            patch("bot.get_active_repo", return_value=None),
        ):
            await _process_message(chat_id, "new message", update, context, cancel=cancel)

        # History should be rolled back
        assert len(conversations[chat_id]) == history_before
        mock_send.assert_called_once()
        assert "cancelled" in mock_send.call_args[0][1].lower()

        # Clean up
        conversations.pop(chat_id, None)
