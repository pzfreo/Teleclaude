"""Tests for the ask_user inline keyboard tool."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAskUserToolSchema:
    """Tests for ASK_USER_TOOL definition."""

    def test_tool_schema_valid(self):
        from bot import ASK_USER_TOOL

        assert ASK_USER_TOOL["name"] == "ask_user"
        schema = ASK_USER_TOOL["input_schema"]
        assert "question" in schema["properties"]
        assert "options" in schema["properties"]
        assert schema["properties"]["options"]["minItems"] == 2
        assert schema["properties"]["options"]["maxItems"] == 5

    def test_tool_in_default_tools(self):
        """ASK_USER_TOOL should always be available."""
        from bot import ASK_USER_TOOL, TODO_TOOL

        assert ASK_USER_TOOL["name"] != TODO_TOOL["name"]


class TestHandleAskUser:
    """Tests for _handle_ask_user()."""

    @pytest.mark.asyncio
    async def test_sends_keyboard(self):
        from bot import _handle_ask_user

        block = MagicMock()
        block.input = {"question": "Pick one:", "options": ["A", "B", "C"]}

        bot = AsyncMock()
        chat_id = 12345

        # Simulate a quick user response
        async def resolve_future():
            await asyncio.sleep(0.05)
            from bot import _ask_user_futures

            future = _ask_user_futures.get(chat_id)
            if future and not future.done():
                future.set_result("B")

        task = asyncio.create_task(resolve_future())
        result = await _handle_ask_user(block, chat_id, bot)
        await task

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args[1]
        assert call_kwargs["chat_id"] == chat_id
        assert call_kwargs["text"] == "Pick one:"
        # Check that reply_markup has 3 rows
        markup = call_kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 3
        assert result == "User selected: B"

    @pytest.mark.asyncio
    async def test_timeout(self):
        from bot import _handle_ask_user

        block = MagicMock()
        block.input = {"question": "Pick:", "options": ["X", "Y"]}

        bot = AsyncMock()
        chat_id = 55555

        with patch("bot.ASK_USER_TIMEOUT", 0.1):
            result = await _handle_ask_user(block, chat_id, bot)

        assert "timeout" in result.lower()

    @pytest.mark.asyncio
    async def test_too_few_options(self):
        from bot import _handle_ask_user

        block = MagicMock()
        block.input = {"question": "Pick:", "options": ["Only one"]}

        bot = AsyncMock()
        result = await _handle_ask_user(block, 12345, bot)
        assert "at least 2" in result.lower()

    @pytest.mark.asyncio
    async def test_options_capped_at_5(self):
        from bot import _handle_ask_user

        block = MagicMock()
        block.input = {"question": "Pick:", "options": ["A", "B", "C", "D", "E", "F", "G"]}

        bot = AsyncMock()
        chat_id = 33333

        async def resolve():
            await asyncio.sleep(0.05)
            from bot import _ask_user_futures

            future = _ask_user_futures.get(chat_id)
            if future and not future.done():
                future.set_result("A")

        task = asyncio.create_task(resolve())
        await _handle_ask_user(block, chat_id, bot)
        await task

        markup = bot.send_message.call_args[1]["reply_markup"]
        assert len(markup.inline_keyboard) == 5


class TestAskUserCallback:
    """Tests for _ask_user_callback()."""

    @pytest.mark.asyncio
    async def test_callback_resolves_future(self):
        from bot import _ask_user_callback, _ask_user_futures

        chat_id = 44444
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        _ask_user_futures[chat_id] = future

        update = MagicMock()
        update.effective_user.id = 12345
        query = AsyncMock()
        query.data = f"ask_user:{chat_id}:1"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock()
        query.message.text = "Pick one:"
        query.message.reply_markup = MagicMock()
        # Simulate 2-row keyboard
        btn0 = MagicMock()
        btn0.text = "Option A"
        btn1 = MagicMock()
        btn1.text = "Option B"
        query.message.reply_markup.inline_keyboard = [[btn0], [btn1]]
        update.callback_query = query

        context = MagicMock()
        with patch("bot.ALLOWED_USER_IDS", set()):
            await _ask_user_callback(update, context)

        assert future.done()
        assert future.result() == "Option B"
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

        _ask_user_futures.pop(chat_id, None)

    @pytest.mark.asyncio
    async def test_callback_ignores_invalid_data(self):
        from bot import _ask_user_callback

        update = MagicMock()
        update.effective_user.id = 12345
        query = AsyncMock()
        query.data = "not_ask_user:123:0"
        update.callback_query = query

        context = MagicMock()
        # Should not raise
        with patch("bot.ALLOWED_USER_IDS", set()):
            await _ask_user_callback(update, context)
