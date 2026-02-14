"""Tests for streaming.py — StreamingResponder and helpers."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streaming import EDIT_THROTTLE_INTERVAL, SPLIT_THRESHOLD, StreamingResponder, _close_unclosed_code_blocks

# ── _close_unclosed_code_blocks tests ────────────────────────────────


class TestCloseUnclodedCodeBlocks:
    def test_no_code_blocks(self):
        assert _close_unclosed_code_blocks("hello world") == "hello world"

    def test_closed_code_block(self):
        text = "```python\nprint('hi')\n```"
        assert _close_unclosed_code_blocks(text) == text

    def test_unclosed_code_block(self):
        text = "```python\nprint('hi')"
        result = _close_unclosed_code_blocks(text)
        assert result.endswith("\n```")
        assert result.count("```") == 2

    def test_multiple_blocks_last_unclosed(self):
        text = "```python\nfoo\n```\n\n```\nbar"
        result = _close_unclosed_code_blocks(text)
        assert result.count("```") == 4  # 3 original + 1 closing

    def test_even_count_unchanged(self):
        text = "```a``` and ```b```"
        assert _close_unclosed_code_blocks(text) == text

    def test_empty_string(self):
        assert _close_unclosed_code_blocks("") == ""


# ── StreamingResponder tests ─────────────────────────────────────────


class TestStreamingResponder:
    async def test_single_chunk_sends_new_message(self):
        """First text chunk sends a new Telegram message."""
        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("Hello world")
        await resp.finalize()

        bot.send_message.assert_called_once()
        assert bot.send_message.call_args.kwargs["chat_id"] == 42
        assert "Hello world" in bot.send_message.call_args.kwargs["text"]

    async def test_throttled_edits(self):
        """Multiple rapid chunks result in throttled edits, not one per chunk."""
        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        resp = StreamingResponder(bot, chat_id=42)

        # Feed many chunks rapidly (monotonic time won't advance enough for second flush)
        for i in range(20):
            await resp.feed(f"word{i} ")

        # Only the first feed triggers a flush (send_message), rest are throttled
        assert bot.send_message.call_count == 1
        # No edits yet because throttle interval hasn't passed
        assert bot.edit_message_text.call_count == 0

        await resp.finalize()
        # Finalize forces one more flush (edit)
        assert bot.edit_message_text.call_count == 1

    async def test_edit_after_throttle_interval(self):
        """After throttle interval elapses, feed triggers an edit."""
        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("Hello")
        assert bot.send_message.call_count == 1

        # Simulate time passing beyond throttle interval
        resp._last_edit_time = time.monotonic() - EDIT_THROTTLE_INTERVAL - 0.1
        await resp.feed(" world")

        # Should have edited the message
        assert bot.edit_message_text.call_count == 1

    async def test_finalize_flushes_remaining(self):
        """finalize() sends text that hasn't been flushed yet."""
        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("Hello")
        # Feed more without waiting for throttle
        await resp.feed(" cruel world")

        # Only one send so far
        assert bot.send_message.call_count == 1
        assert bot.edit_message_text.call_count == 0

        await resp.finalize()
        # finalize should have edited with the full text
        assert bot.edit_message_text.call_count == 1
        edit_text = bot.edit_message_text.call_args.kwargs["text"]
        assert "Hello cruel world" in edit_text

    async def test_message_splitting(self):
        """When text exceeds SPLIT_THRESHOLD, a new message is started."""
        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        resp = StreamingResponder(bot, chat_id=42)

        # Feed text in two chunks with throttle interval between them
        await resp.feed("x" * SPLIT_THRESHOLD)
        assert bot.send_message.call_count == 1

        # Simulate throttle interval passing, then feed more
        resp._last_edit_time = time.monotonic() - EDIT_THROTTLE_INTERVAL - 0.1
        await resp.feed("y" * 200)

        # Split should have triggered a second send_message
        assert bot.send_message.call_count == 2

    async def test_edit_failure_triggers_fallback(self):
        """BadRequest('message to edit not found') triggers fallback mode."""
        from telegram.error import BadRequest

        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("Hello")
        # Simulate time passing
        resp._last_edit_time = time.monotonic() - EDIT_THROTTLE_INTERVAL - 0.1
        await resp.feed(" world")

        # Edit was attempted and failed
        assert bot.edit_message_text.call_count == 1
        assert resp._failed is True

        # Further flushes do nothing
        resp._last_edit_time = time.monotonic() - EDIT_THROTTLE_INTERVAL - 0.1
        await resp.feed(" more")
        assert bot.edit_message_text.call_count == 1  # no new edits

    async def test_message_not_modified_ignored(self):
        """'message is not modified' BadRequest is silently ignored."""
        from telegram.error import BadRequest

        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        bot.edit_message_text.side_effect = BadRequest("Message is not modified")
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("Hello")
        resp._last_edit_time = time.monotonic() - EDIT_THROTTLE_INTERVAL - 0.1
        await resp.feed(" world")

        # Edit was attempted — but "not modified" is not a failure
        assert resp._failed is False

    async def test_full_text_property(self):
        """full_text returns all accumulated text."""
        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("Hello")
        await resp.feed(" world")
        await resp.feed("!")

        assert resp.full_text == "Hello world!"

    async def test_empty_stream_no_messages(self):
        """If no text is fed, finalize sends nothing."""
        bot = AsyncMock()
        resp = StreamingResponder(bot, chat_id=42)

        await resp.finalize()

        bot.send_message.assert_not_called()
        bot.edit_message_text.assert_not_called()

    async def test_whitespace_only_no_message(self):
        """Whitespace-only text does not trigger a send."""
        bot = AsyncMock()
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("   ")
        await resp.finalize()

        bot.send_message.assert_not_called()

    async def test_finalize_idempotent(self):
        """Calling finalize() twice does not send duplicate messages."""
        bot = AsyncMock()
        bot.send_message.return_value = MagicMock(message_id=100)
        resp = StreamingResponder(bot, chat_id=42)

        await resp.feed("Hello")
        await resp.finalize()
        send_count = bot.send_message.call_count

        await resp.finalize()
        assert bot.send_message.call_count == send_count


# ── _stream_round tests ──────────────────────────────────────────────


class MockDelta:
    """Mock for stream event delta."""

    def __init__(self, delta_type, text=""):
        self.type = delta_type
        self.text = text


class MockContentBlock:
    """Mock for content_block_start event."""

    def __init__(self, block_type):
        self.type = block_type


class MockEvent:
    """Mock for a stream event."""

    def __init__(self, event_type, delta=None, content_block=None):
        self.type = event_type
        self.delta = delta
        self.content_block = content_block


class MockStream:
    """Mock for async_api_client.messages.stream() context manager."""

    def __init__(self, events, final_message):
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self._iter_events()

    async def _iter_events(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final_message


class TestStreamRound:
    async def test_text_response_streams(self):
        """Pure text response is streamed via StreamingResponder."""
        import asyncio

        from bot import _stream_round

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello world!"
        final_msg = MagicMock()
        final_msg.stop_reason = "end_turn"
        final_msg.content = [text_block]

        events = [
            MockEvent("content_block_start", content_block=MockContentBlock("text")),
            MockEvent("content_block_delta", delta=MockDelta("text_delta", "Hello ")),
            MockEvent("content_block_delta", delta=MockDelta("text_delta", "world!")),
            MockEvent("content_block_stop"),
            MockEvent("message_stop"),
        ]
        mock_stream = MockStream(events, final_msg)
        stop_typing = asyncio.Event()

        with patch("bot.async_api_client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream
            response, streamed_text = await _stream_round(
                {"model": "test", "messages": []}, chat_id=42, bot=AsyncMock(), stop_typing=stop_typing
            )

        assert response.stop_reason == "end_turn"
        assert streamed_text == "Hello world!"
        assert stop_typing.is_set()

    async def test_tool_use_response_no_streaming(self):
        """Tool_use response returns message with no streamed text."""
        import asyncio

        from bot import _stream_round

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "web_search"
        tool_block.input = {"query": "test"}
        tool_block.id = "tool_123"
        final_msg = MagicMock()
        final_msg.stop_reason = "tool_use"
        final_msg.content = [tool_block]

        events = [
            MockEvent("content_block_start", content_block=MockContentBlock("tool_use")),
            MockEvent("content_block_delta", delta=MockDelta("input_json_delta")),
            MockEvent("content_block_stop"),
            MockEvent("message_stop"),
        ]
        mock_stream = MockStream(events, final_msg)
        stop_typing = asyncio.Event()

        with patch("bot.async_api_client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream
            response, streamed_text = await _stream_round(
                {"model": "test", "messages": []}, chat_id=42, bot=AsyncMock(), stop_typing=stop_typing
            )

        assert response.stop_reason == "tool_use"
        assert streamed_text is None

    async def test_mixed_text_and_tool_use(self):
        """Response with text + tool_use streams the text portion."""
        import asyncio

        from bot import _stream_round

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Let me search."
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "web_search"
        tool_block.input = {"query": "test"}
        tool_block.id = "tool_456"
        final_msg = MagicMock()
        final_msg.stop_reason = "tool_use"
        final_msg.content = [text_block, tool_block]

        events = [
            MockEvent("content_block_start", content_block=MockContentBlock("text")),
            MockEvent("content_block_delta", delta=MockDelta("text_delta", "Let me search.")),
            MockEvent("content_block_stop"),
            MockEvent("content_block_start", content_block=MockContentBlock("tool_use")),
            MockEvent("content_block_delta", delta=MockDelta("input_json_delta")),
            MockEvent("content_block_stop"),
            MockEvent("message_stop"),
        ]
        mock_stream = MockStream(events, final_msg)
        stop_typing = asyncio.Event()

        with patch("bot.async_api_client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream
            response, streamed_text = await _stream_round(
                {"model": "test", "messages": []}, chat_id=42, bot=AsyncMock(), stop_typing=stop_typing
            )

        assert response.stop_reason == "tool_use"
        assert streamed_text == "Let me search."
        assert stop_typing.is_set()

    async def test_stops_typing_on_first_text(self):
        """Typing indicator is stopped when first text delta arrives."""
        import asyncio

        from bot import _stream_round

        final_msg = MagicMock()
        final_msg.stop_reason = "end_turn"
        final_msg.content = [MagicMock(type="text", text="Hi")]

        events = [
            MockEvent("content_block_delta", delta=MockDelta("text_delta", "Hi")),
            MockEvent("message_stop"),
        ]
        mock_stream = MockStream(events, final_msg)
        stop_typing = asyncio.Event()

        assert not stop_typing.is_set()
        with patch("bot.async_api_client") as mock_client:
            mock_client.messages.stream.return_value = mock_stream
            await _stream_round({"model": "test", "messages": []}, chat_id=42, bot=AsyncMock(), stop_typing=stop_typing)

        assert stop_typing.is_set()

    async def test_rate_limit_raises(self):
        """RateLimitError during stream is raised for caller to handle."""
        import asyncio

        import anthropic

        from bot import _stream_round

        stop_typing = asyncio.Event()
        err = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )

        with patch("bot.async_api_client") as mock_client:
            mock_client.messages.stream.side_effect = err
            with pytest.raises(anthropic.RateLimitError):
                await _stream_round(
                    {"model": "test", "messages": []}, chat_id=42, bot=AsyncMock(), stop_typing=stop_typing
                )
