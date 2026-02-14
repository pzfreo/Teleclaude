"""Tests for bot.py — message handlers, tool dispatch, and core processing logic."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

# ── Helpers ──────────────────────────────────────────────────────────

# Reusable RateLimitError that forces _process_message to fall back to _call_anthropic
_STREAM_FALLBACK_ERROR = anthropic.RateLimitError(
    message="mock", response=MagicMock(status_code=429, headers={}), body=None
)


def _patch_stream_fallback():
    """Patch _stream_round to raise RateLimitError so tests exercise the non-streaming path."""
    return patch("bot._stream_round", new_callable=AsyncMock, side_effect=_STREAM_FALLBACK_ERROR)


# ── Helpers to build mock Telegram Update objects ──────────────────────


def _make_update(chat_id=1001, user_id=42, text="hello", message=None):
    """Create a minimal mock Update for handler tests."""
    update = MagicMock()
    # effective_chat needs AsyncMock because keep_typing calls chat.send_action() which is awaited
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


def _make_context(bot=None, args=None):
    """Create a minimal mock ContextTypes.DEFAULT_TYPE."""
    ctx = MagicMock()
    ctx.bot = bot or AsyncMock()
    ctx.args = args or []
    return ctx


# ── _call_anthropic tests ─────────────────────────────────────────────


class TestCallAnthropic:
    """Test retry logic in _call_anthropic."""

    async def test_success_first_try(self):

        from bot import _call_anthropic

        mock_response = MagicMock()
        with patch("bot.api_client") as mock_client:
            mock_client.messages.create.return_value = mock_response
            result = await _call_anthropic(model="test", max_tokens=100, messages=[])
        assert result is mock_response

    async def test_retries_on_rate_limit(self):
        import anthropic

        from bot import _call_anthropic

        mock_response = MagicMock()
        err = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        with (
            patch("bot.api_client") as mock_client,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_client.messages.create.side_effect = [err, mock_response]
            result = await _call_anthropic(model="test", max_tokens=100, messages=[])
        assert result is mock_response
        assert mock_client.messages.create.call_count == 2

    async def test_retries_on_internal_server_error(self):
        import anthropic

        from bot import _call_anthropic

        mock_response = MagicMock()
        err = anthropic.InternalServerError(
            message="overloaded",
            response=MagicMock(status_code=500, headers={}),
            body=None,
        )
        with (
            patch("bot.api_client") as mock_client,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_client.messages.create.side_effect = [err, mock_response]
            result = await _call_anthropic(model="test", max_tokens=100, messages=[])
        assert result is mock_response

    async def test_raises_after_max_retries(self):
        import anthropic

        from bot import _call_anthropic

        err = anthropic.RateLimitError(
            message="rate limited",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        with (
            patch("bot.api_client") as mock_client,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_client.messages.create.side_effect = err
            with pytest.raises(anthropic.RateLimitError):
                await _call_anthropic(model="test", max_tokens=100, messages=[])
        # 1 initial + 3 retries = 4 attempts
        assert mock_client.messages.create.call_count == 4


# ── _execute_tool_call tests ──────────────────────────────────────────


class TestExecuteToolCall:
    """Test tool dispatch routing in _execute_tool_call."""

    def _make_block(self, name, tool_input):
        block = SimpleNamespace()
        block.name = name
        block.input = tool_input
        block.id = "tool_123"
        return block

    def test_update_todo_list(self):
        from bot import _execute_tool_call, chat_todos

        block = self._make_block("update_todo_list", {"todos": [{"content": "Test", "status": "pending"}]})
        with patch("bot.save_todos"):
            result = _execute_tool_call(block, "owner/repo", 9999)
        assert "Test" in result
        assert chat_todos.get(9999) == [{"content": "Test", "status": "pending"}]
        # Clean up
        chat_todos.pop(9999, None)

    def test_web_search_dispatch(self):
        from bot import _execute_tool_call

        block = self._make_block("web_search", {"query": "test query"})
        with (
            patch("bot.execute_web_tool", return_value="search results"),
            patch("bot.web_client", MagicMock()),
        ):
            result = _execute_tool_call(block, "owner/repo", 9999)
        assert result == "search results"

    def test_github_tool_no_repo(self):
        from bot import _execute_tool_call

        block = self._make_block("get_file", {"path": "test.py"})
        with patch("bot.execute_github_tool", MagicMock()), patch("bot._github_tool_names", {"get_file"}):
            result = _execute_tool_call(block, None, 9999)
        assert "No active repo" in result

    def test_github_tool_with_repo(self):
        from bot import _execute_tool_call

        block = self._make_block("get_file", {"path": "test.py"})
        with (
            patch("bot.execute_github_tool", return_value='{"content": "code"}'),
            patch("bot._github_tool_names", {"get_file"}),
            patch("bot.gh_client", MagicMock()),
        ):
            result = _execute_tool_call(block, "owner/repo", 9999)
        assert result == '{"content": "code"}'

    def test_unavailable_tool(self):
        from bot import _execute_tool_call

        block = self._make_block("nonexistent_tool", {})
        result = _execute_tool_call(block, "owner/repo", 9999)
        assert "not available" in result

    def test_tool_exception_caught(self):
        from bot import _execute_tool_call

        block = self._make_block("web_search", {"query": "test"})
        with (
            patch("bot.execute_web_tool", side_effect=RuntimeError("boom")),
            patch("bot.web_client", MagicMock()),
        ):
            result = _execute_tool_call(block, "owner/repo", 9999)
        assert "Tool error" in result

    def test_create_branch_auto_tracks(self):
        from bot import _execute_tool_call, active_branches

        block = self._make_block("create_branch", {"branch_name": "feat-new", "base": "main"})
        with (
            patch("bot.execute_github_tool", return_value='{"ref": "refs/heads/feat-new"}'),
            patch("bot._github_tool_names", {"create_branch"}),
            patch("bot.gh_client", MagicMock()),
            patch("bot.save_active_branch"),
        ):
            _execute_tool_call(block, "owner/repo", 9998)
        assert active_branches.get(9998) == "feat-new"
        # Clean up
        active_branches.pop(9998, None)


# ── get_* cache functions ─────────────────────────────────────────────


class TestCacheFunctions:
    """Test in-memory cache + DB fallback pattern."""

    def test_get_model_from_cache(self):
        from bot import chat_models, get_model

        chat_models[8888] = "claude-test"
        try:
            assert get_model(8888) == "claude-test"
        finally:
            chat_models.pop(8888, None)

    def test_get_model_from_db(self):
        from bot import chat_models, get_model

        chat_models.pop(8887, None)
        with patch("bot.load_model", return_value="claude-db"):
            assert get_model(8887) == "claude-db"
        chat_models.pop(8887, None)

    def test_get_model_default(self):
        from bot import chat_models, get_model

        chat_models.pop(8886, None)
        with patch("bot.load_model", return_value=None):
            result = get_model(8886)
        assert result.startswith("claude-")
        chat_models.pop(8886, None)

    def test_get_conversation_loads_from_db(self):
        from bot import conversations, get_conversation

        conversations.pop(8885, None)
        msgs = [{"role": "user", "content": "hello"}]
        with patch("bot.load_conversation", return_value=msgs):
            result = get_conversation(8885)
        assert len(result) >= 1
        conversations.pop(8885, None)

    def test_get_active_repo_cache(self):
        from bot import active_repos, get_active_repo

        active_repos[8884] = "owner/repo"
        try:
            assert get_active_repo(8884) == "owner/repo"
        finally:
            active_repos.pop(8884, None)

    def test_get_active_repo_none(self):
        from bot import active_repos, get_active_repo

        active_repos.pop(8883, None)
        with patch("bot.load_active_repo", return_value=None):
            assert get_active_repo(8883) is None

    def test_get_todos_cache_and_db(self):
        from bot import chat_todos, get_todos

        chat_todos.pop(8882, None)
        with patch("bot.load_todos", return_value=[{"content": "task", "status": "pending"}]):
            result = get_todos(8882)
        assert len(result) == 1
        chat_todos.pop(8882, None)

    def test_get_plan_mode_default(self):
        from bot import chat_plan_mode, get_plan_mode

        chat_plan_mode.pop(8881, None)
        with patch("bot.load_plan_mode", return_value=False):
            assert get_plan_mode(8881) is False
        chat_plan_mode.pop(8881, None)


# ── Command handler tests ─────────────────────────────────────────────


class TestCommandHandlers:
    """Test Telegram command handlers (mocked Telegram API)."""

    async def test_start_authorized(self):
        from bot import start

        update = _make_update()
        ctx = _make_context()
        with patch("bot.is_authorized", return_value=True):
            await start(update, ctx)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "Teleclaude" in text

    async def test_start_unauthorized(self):
        from bot import start

        update = _make_update()
        ctx = _make_context()
        with patch("bot.is_authorized", return_value=False):
            await start(update, ctx)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "not authorized" in text

    async def test_new_conversation_clears_state(self):
        from bot import conversations, new_conversation

        update = _make_update(chat_id=7777)
        ctx = _make_context()
        conversations[7777] = [{"role": "user", "content": "old"}]
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.clear_conversation"),
            patch("bot.save_todos"),
            patch("bot.save_plan_mode"),
            patch("bot.save_active_branch"),
        ):
            await new_conversation(update, ctx)
        assert conversations[7777] == []
        update.message.reply_text.assert_called_once()
        assert "cleared" in update.message.reply_text.call_args[0][0].lower()
        conversations.pop(7777, None)

    async def test_show_model_no_args(self):
        from bot import show_model

        update = _make_update(chat_id=7776)
        ctx = _make_context(args=[])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.get_model", return_value="claude-sonnet-4-5-20250929"),
        ):
            await show_model(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "claude-sonnet" in text

    async def test_show_model_switch_shortcut(self):
        from bot import chat_models, show_model

        update = _make_update(chat_id=7775)
        ctx = _make_context(args=["opus"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.save_model"),
        ):
            await show_model(update, ctx)
        assert chat_models.get(7775) == "claude-opus-4-6"
        text = update.message.reply_text.call_args[0][0]
        assert "opus" in text.lower()
        chat_models.pop(7775, None)

    async def test_show_model_invalid(self):
        from bot import show_model

        update = _make_update()
        ctx = _make_context(args=["gpt-4"])
        with patch("bot.is_authorized", return_value=True):
            await show_model(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Unknown model" in text

    async def test_toggle_plan(self):
        from bot import chat_plan_mode, toggle_plan

        update = _make_update(chat_id=7774)
        ctx = _make_context()
        chat_plan_mode.pop(7774, None)
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.get_plan_mode", return_value=False),
            patch("bot.save_plan_mode"),
        ):
            await toggle_plan(update, ctx)
        assert chat_plan_mode.get(7774) is True
        text = update.message.reply_text.call_args[0][0]
        assert "ON" in text
        chat_plan_mode.pop(7774, None)

    async def test_show_todos_empty(self):
        from bot import show_todos

        update = _make_update(chat_id=7773)
        ctx = _make_context(args=[])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.get_todos", return_value=[]),
        ):
            await show_todos(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No tasks" in text

    async def test_show_todos_clear(self):
        from bot import chat_todos, show_todos

        update = _make_update(chat_id=7772)
        ctx = _make_context(args=["clear"])
        chat_todos[7772] = [{"content": "task", "status": "pending"}]
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.save_todos"),
        ):
            await show_todos(update, ctx)
        assert chat_todos[7772] == []
        text = update.message.reply_text.call_args[0][0]
        assert "cleared" in text.lower()
        chat_todos.pop(7772, None)

    async def test_show_version(self):
        from bot import show_version

        update = _make_update()
        ctx = _make_context()
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.get_model", return_value="claude-test"),
        ):
            await show_version(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Teleclaude" in text

    async def test_unknown_command(self):
        from bot import unknown_command

        update = _make_update(text="/foobar")
        ctx = _make_context()
        with patch("bot.is_authorized", return_value=True):
            await unknown_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Unknown command" in text


# ── handle_message tests ──────────────────────────────────────────────


class TestHandleMessage:
    """Test the main message handler entry point."""

    async def test_unauthorized_user_rejected(self):
        from bot import handle_message

        update = _make_update()
        ctx = _make_context()
        with patch("bot.is_authorized", return_value=False):
            await handle_message(update, ctx)
        update.message.reply_text.assert_called_once()
        assert "not authorized" in update.message.reply_text.call_args[0][0]

    async def test_empty_content_skipped(self):
        from bot import handle_message

        update = _make_update(text="")
        update.message.text = None
        update.message.caption = None
        ctx = _make_context()
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot._build_user_content", new_callable=AsyncMock, return_value=None),
        ):
            await handle_message(update, ctx)
        # No _process_message should be called

    async def test_message_acquires_lock(self):
        from bot import handle_message

        update = _make_update(chat_id=6666)
        ctx = _make_context()
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot._build_user_content", new_callable=AsyncMock, return_value="test"),
            patch("bot._process_message", new_callable=AsyncMock) as mock_process,
            patch("bot.audit_log"),
        ):
            await handle_message(update, ctx)
        mock_process.assert_called_once()

    async def test_no_message_noop(self):
        from bot import handle_message

        update = MagicMock()
        update.message = None
        ctx = _make_context()
        await handle_message(update, ctx)  # should not raise


# ── _process_message tests ────────────────────────────────────────────


class TestProcessMessage:
    """Test the core _process_message loop with mocked Anthropic API."""

    async def test_simple_text_response(self):
        from bot import _process_message, conversations

        conversations[5555] = []
        update = _make_update(chat_id=5555)
        ctx = _make_context()

        # Mock a simple text response (no tool use)
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Hello! How can I help?"
        mock_response.content = [text_block]

        with (
            _patch_stream_fallback(),
            patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response),
            patch("bot.save_state"),
            patch("bot.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot.get_active_repo", return_value=None),
            patch("bot.get_model", return_value="claude-test"),
            patch("bot.get_plan_mode", return_value=False),
        ):
            await _process_message(5555, "hello", update, ctx)

        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        assert "Hello! How can I help?" in sent_text
        conversations.pop(5555, None)

    async def test_tool_use_loop(self):
        from bot import _process_message, conversations

        conversations[5554] = []
        update = _make_update(chat_id=5554)
        ctx = _make_context()

        # First response: tool_use
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "web_search"
        tool_block.input = {"query": "test"}
        tool_block.id = "tool_abc"
        resp1 = MagicMock()
        resp1.stop_reason = "tool_use"
        resp1.content = [tool_block]

        # Second response: text
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Here are the results."
        resp2 = MagicMock()
        resp2.stop_reason = "end_turn"
        resp2.content = [text_block]

        with (
            _patch_stream_fallback(),
            patch("bot._call_anthropic", new_callable=AsyncMock, side_effect=[resp1, resp2]),
            patch("bot._execute_tool_call", return_value="search results here"),
            patch("bot.save_state"),
            patch("bot.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot.get_active_repo", return_value="owner/repo"),
            patch("bot.get_active_branch", return_value=None),
            patch("bot.get_model", return_value="claude-test"),
            patch("bot.get_plan_mode", return_value=False),
        ):
            await _process_message(5554, "search for test", update, ctx)

        mock_send.assert_called_once()
        assert "results" in mock_send.call_args[0][1].lower()
        conversations.pop(5554, None)

    async def test_tool_result_truncation(self):
        """Tool results > 10000 chars should be truncated."""
        from bot import _process_message, conversations

        conversations[5553] = []
        update = _make_update(chat_id=5553)
        ctx = _make_context()

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "get_file"
        tool_block.input = {"path": "big.py"}
        tool_block.id = "tool_big"
        resp1 = MagicMock()
        resp1.stop_reason = "tool_use"
        resp1.content = [tool_block]

        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Done"
        resp2 = MagicMock()
        resp2.stop_reason = "end_turn"
        resp2.content = [text_block]

        big_result = "x" * 20000

        with (
            _patch_stream_fallback(),
            patch("bot._call_anthropic", new_callable=AsyncMock, side_effect=[resp1, resp2]),
            patch("bot._execute_tool_call", return_value=big_result),
            patch("bot.save_state"),
            patch("bot.send_long_message", new_callable=AsyncMock),
            patch("bot.get_active_repo", return_value="owner/repo"),
            patch("bot.get_active_branch", return_value=None),
            patch("bot.get_model", return_value="claude-test"),
            patch("bot.get_plan_mode", return_value=False),
        ):
            await _process_message(5553, "read big file", update, ctx)

        # Verify history contains truncated result
        history = conversations[5553]
        # Find the tool_result message
        for msg in history:
            if msg["role"] == "user" and isinstance(msg["content"], list):
                for block in msg["content"]:
                    if block.get("type") == "tool_result":
                        assert len(block["content"]) <= 10020  # 10000 + truncation msg
                        assert "truncated" in block["content"]
        conversations.pop(5553, None)

    async def test_api_error_rolls_back(self):
        """On API error, history should be rolled back."""
        import anthropic

        from bot import _process_message, conversations

        conversations[5552] = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old reply"}]
        original_len = len(conversations[5552])
        update = _make_update(chat_id=5552)
        ctx = _make_context()

        err = anthropic.APIError(
            message="bad request",
            request=MagicMock(),
            body=None,
        )

        with (
            _patch_stream_fallback(),
            patch("bot._call_anthropic", new_callable=AsyncMock, side_effect=err),
            patch("bot.save_state"),
            patch("bot.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot.get_active_repo", return_value=None),
            patch("bot.get_model", return_value="claude-test"),
            patch("bot.get_plan_mode", return_value=False),
        ):
            await _process_message(5552, "trigger error", update, ctx)

        # History should be rolled back
        assert len(conversations[5552]) == original_len
        # Error message should be sent
        sent = mock_send.call_args[0][1]
        assert "error" in sent.lower() or "Error" in sent
        conversations.pop(5552, None)

    async def test_max_tool_rounds_reached(self):
        """After MAX_TOOL_ROUNDS, a limit message should be sent."""
        from bot import _process_message, conversations

        conversations[5551] = []
        update = _make_update(chat_id=5551)
        ctx = _make_context()

        # Every response is tool_use, forever
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "web_search"
        tool_block.input = {"query": "loop"}
        tool_block.id = "tool_loop"
        resp = MagicMock()
        resp.stop_reason = "tool_use"
        resp.content = [tool_block]

        with (
            _patch_stream_fallback(),
            patch("bot._call_anthropic", new_callable=AsyncMock, return_value=resp),
            patch("bot._execute_tool_call", return_value="result"),
            patch("bot.save_state"),
            patch("bot.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot.get_active_repo", return_value="owner/repo"),
            patch("bot.get_active_branch", return_value=None),
            patch("bot.get_model", return_value="claude-test"),
            patch("bot.get_plan_mode", return_value=False),
            patch("bot.MAX_TOOL_ROUNDS", 3),
        ):
            await _process_message(5551, "loop forever", update, ctx)

        sent = mock_send.call_args[0][1]
        assert "tool call limit" in sent.lower() or "limit" in sent.lower()
        conversations.pop(5551, None)


# ── trim_history tests ────────────────────────────────────────────────


class TestTrimHistory:
    def test_trims_to_max(self):
        from bot import conversations, trim_history

        # Create history longer than MAX_HISTORY * 2
        long_history = []
        for i in range(200):
            long_history.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"})
        conversations[4444] = long_history

        with patch("bot.save_conversation"):
            trim_history(4444)

        from bot import MAX_HISTORY

        assert len(conversations[4444]) <= MAX_HISTORY * 2
        conversations.pop(4444, None)

    def test_strips_images_from_old_messages(self):
        from bot import conversations, trim_history

        history = []
        for i in range(20):
            if i % 2 == 0:
                history.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "data": "abc"}},
                            {"type": "text", "text": f"msg {i}"},
                        ],
                    }
                )
            else:
                history.append({"role": "assistant", "content": f"reply {i}"})
        conversations[4443] = history

        with patch("bot.save_conversation"):
            trim_history(4443)

        # Old messages (outside last 10) should have images stripped
        result = conversations[4443]
        for i, msg in enumerate(result):
            if i < max(0, len(result) - 10):
                if isinstance(msg.get("content"), list):
                    for block in msg["content"]:
                        assert block.get("type") != "image"
        conversations.pop(4443, None)


# ── keep_typing tests ─────────────────────────────────────────────────


class TestKeepTyping:
    async def test_stops_on_event(self):
        import time

        from bot import keep_typing

        chat = AsyncMock()
        bot = AsyncMock()
        stop = asyncio.Event()
        status = {"round": 0, "max": 15, "tools": []}
        start = time.time()

        # Set stop immediately
        stop.set()
        await keep_typing(chat, stop, start, bot, status)
        # Should complete quickly without hanging


# ── _build_user_content tests ─────────────────────────────────────────


class TestBuildUserContent:
    async def test_text_only(self):
        from bot import _build_user_content

        update = _make_update(text="hello world")
        bot = AsyncMock()
        result = await _build_user_content(update, bot)
        assert result == "hello world"

    async def test_voice_message_unsupported(self):
        from bot import _build_user_content

        update = _make_update(text="")
        update.message.text = None
        update.message.caption = None
        update.message.voice = MagicMock()
        bot = AsyncMock()
        result = await _build_user_content(update, bot)
        assert "voice" in result.lower()

    async def test_location_included(self):
        from bot import _build_user_content

        update = _make_update(text="")
        update.message.text = None
        update.message.caption = None
        loc = MagicMock()
        loc.latitude = 40.7128
        loc.longitude = -74.0060
        update.message.location = loc
        bot = AsyncMock()
        result = await _build_user_content(update, bot)
        assert "40.7128" in result

    async def test_contact_included(self):
        from bot import _build_user_content

        update = _make_update(text="")
        update.message.text = None
        update.message.caption = None
        contact = MagicMock()
        contact.first_name = "John"
        contact.last_name = "Doe"
        contact.phone_number = "+1234567890"
        update.message.contact = contact
        bot = AsyncMock()
        result = await _build_user_content(update, bot)
        assert "John" in result
        assert "+1234567890" in result

    async def test_empty_message_returns_none(self):
        from bot import _build_user_content

        update = _make_update(text="")
        update.message.text = None
        update.message.caption = None
        bot = AsyncMock()
        result = await _build_user_content(update, bot)
        assert result is None

    async def test_photo_creates_image_block(self):
        from bot import _build_user_content

        update = _make_update(text="check this")
        update.message.photo = [MagicMock(), MagicMock()]  # multiple sizes

        bot = AsyncMock()
        with patch("bot._download_telegram_file", new_callable=AsyncMock, return_value=b"\x89PNG"):
            result = await _build_user_content(update, bot)

        assert isinstance(result, list)
        assert any(b.get("type") == "image" for b in result)
        assert any(b.get("type") == "text" for b in result)


# ── send_long_message tests ───────────────────────────────────────────


class TestSendLongMessage:
    async def test_short_message_single_send(self):
        from shared import send_long_message

        bot = AsyncMock()
        await send_long_message(1001, "hello", bot)
        bot.send_message.assert_called_once()

    async def test_long_message_split(self):
        from shared import MAX_TELEGRAM_LENGTH, send_long_message

        bot = AsyncMock()
        long_text = "x" * (MAX_TELEGRAM_LENGTH * 2 + 100)
        await send_long_message(1001, long_text, bot)
        assert bot.send_message.call_count == 3

    async def test_empty_message_noop(self):
        from shared import send_long_message

        bot = AsyncMock()
        await send_long_message(1001, "", bot)
        bot.send_message.assert_not_called()
