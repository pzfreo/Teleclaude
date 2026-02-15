"""Tests for bot_agent.py — command handlers and message handling."""

from unittest.mock import AsyncMock, MagicMock, patch


class TestFormatToolProgress:
    """Test _format_tool_progress() formatting."""

    def test_read_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Read", "input": {"file_path": "/app/src/main.py"}})
        assert result == "Reading app/src/main.py"

    def test_write_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Write", "input": {"file_path": "/app/src/main.py"}})
        assert result == "Writing app/src/main.py"

    def test_edit_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Edit", "input": {"file_path": "/a/b/c.py"}})
        assert result == "Editing a/b/c.py"

    def test_bash_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Bash", "input": {"command": "npm test"}})
        assert result == "$ npm test"

    def test_bash_multiline_truncated(self):
        from bot_agent import _format_tool_progress

        long_cmd = "a" * 100 + "\nsecond line"
        result = _format_tool_progress({"name": "Bash", "input": {"command": long_cmd}})
        assert result.startswith("$ ")
        assert len(result) <= 82  # "$ " + 80 chars

    def test_glob_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Glob", "input": {"pattern": "**/*.py"}})
        assert result == "Finding **/*.py"

    def test_grep_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Grep", "input": {"pattern": "TODO"}})
        assert result == "Searching: TODO"

    def test_task_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Task", "input": {"description": "explore code"}})
        assert result == "Subagent: explore code"

    def test_unknown_tool_fallback(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "web_fetch", "input": {}})
        assert result == "Web Fetch"

    def test_empty_name(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "", "input": {}})
        assert result is None

    def test_empty_input(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Read", "input": {}})
        assert result is None


class TestShortPath:
    def test_long_path(self):
        from bot_agent import _short_path

        # _short_path keeps last 3 components for paths with >3 parts
        assert _short_path("/home/user/projects/src/main.py") == "projects/src/main.py"

    def test_short_path(self):
        from bot_agent import _short_path

        assert _short_path("src/main.py") == "src/main.py"

    def test_single_component(self):
        from bot_agent import _short_path

        assert _short_path("file.py") == "file.py"


class TestSaveAttachment:
    def test_sanitizes_path_traversal_in_label(self, tmp_path):
        with (patch("bot_agent.claude_code_mgr") as mock_mgr,):
            mock_mgr.workspace_root = tmp_path
            from bot_agent import _save_attachment

            path = _save_attachment(123, b"hello", "text/plain", "../../etc/passwd")
            # The path traversal characters should be sanitized (replaced with _)
            assert ".." not in path
            # File should be inside the shared dir, not outside it
            shared_dir = str((tmp_path / ".shared" / "123").resolve())
            assert path.startswith(shared_dir)

    def test_saves_file(self, tmp_path):
        with patch("bot_agent.claude_code_mgr") as mock_mgr:
            mock_mgr.workspace_root = tmp_path
            from bot_agent import _save_attachment

            path = _save_attachment(42, b"test data", "image/jpeg", "photo")
            import os

            assert os.path.exists(path)
            with open(path, "rb") as f:
                assert f.read() == b"test data"


# ── Helpers for async tests ───────────────────────────────────────────


def _make_update(chat_id=1001, user_id=42, text="hello"):
    """Create a minimal mock Update for handler tests."""
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
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
    return update


def _make_context(bot=None, args=None):
    ctx = MagicMock()
    ctx.bot = bot or AsyncMock()
    ctx.args = args or []
    return ctx


# ── _run_cli tests ────────────────────────────────────────────────────


class TestRunCli:
    """Test the _run_cli function that wraps Claude Code CLI calls."""

    async def test_no_repo_sends_error(self):
        from bot_agent import _run_cli

        update = _make_update(chat_id=3333)
        ctx = _make_context()
        with patch("bot_agent.get_active_repo", return_value=None):
            await _run_cli(3333, "hello", update, ctx)
        update.message.reply_text.assert_called_once()
        assert "No repo" in update.message.reply_text.call_args[0][0]

    async def test_successful_run(self):
        from bot_agent import _run_cli

        update = _make_update(chat_id=3332)
        ctx = _make_context()
        with (
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_model", return_value="claude-test"),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot_agent.save_conversation"),
            patch("bot_agent.load_conversation", return_value=[]),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.load_session_id", return_value=None),
        ):
            mock_mgr.run = AsyncMock(return_value="CLI output here")
            await _run_cli(3332, "do something", update, ctx)
        mock_send.assert_called_once()
        assert "CLI output here" in mock_send.call_args[0][1]

    async def test_empty_result_shows_no_output(self):
        from bot_agent import _run_cli

        update = _make_update(chat_id=3331)
        ctx = _make_context()
        with (
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_model", return_value="claude-test"),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot_agent.save_conversation"),
            patch("bot_agent.load_conversation", return_value=[]),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.load_session_id", return_value=None),
        ):
            mock_mgr.run = AsyncMock(return_value="")
            await _run_cli(3331, "do something", update, ctx)
        sent = mock_send.call_args[0][1]
        assert "no output" in sent.lower()

    async def test_cli_error_caught(self):
        from bot_agent import _run_cli

        update = _make_update(chat_id=3330)
        ctx = _make_context()
        with (
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_model", return_value="claude-test"),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot_agent.save_conversation"),
            patch("bot_agent.load_conversation", return_value=[]),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.load_session_id", return_value=None),
        ):
            mock_mgr.run = AsyncMock(side_effect=RuntimeError("CLI crashed"))
            await _run_cli(3330, "do something", update, ctx)
        sent = mock_send.call_args[0][1]
        assert "error" in sent.lower()

    async def test_saves_conversation_after_run(self):
        from bot_agent import _run_cli

        update = _make_update(chat_id=3329)
        ctx = _make_context()
        with (
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_model", return_value="claude-test"),
            patch("bot_agent.get_active_branch", return_value="main"),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.send_long_message", new_callable=AsyncMock),
            patch("bot_agent.save_conversation") as mock_save,
            patch("bot_agent.load_conversation", return_value=[]),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.load_session_id", return_value=None),
        ):
            mock_mgr.run = AsyncMock(return_value="result")
            await _run_cli(3329, "prompt", update, ctx)
        mock_save.assert_called_once()
        saved_msgs = mock_save.call_args[0][1]
        assert saved_msgs[-2]["role"] == "user"
        assert saved_msgs[-1]["role"] == "assistant"

    async def test_restores_session_from_db(self):
        """Session ID should be restored from DB when not in memory."""
        from bot_agent import _run_cli

        update = _make_update(chat_id=3328)
        ctx = _make_context()
        with (
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_model", return_value="claude-test"),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.send_long_message", new_callable=AsyncMock),
            patch("bot_agent.save_conversation"),
            patch("bot_agent.load_conversation", return_value=[]),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.load_session_id", return_value="saved-session-xyz"),
        ):
            mock_mgr.get_session_id.return_value = None
            mock_mgr._sessions = {}
            mock_mgr.run = AsyncMock(return_value="result")
            await _run_cli(3328, "prompt", update, ctx)
        # Session should have been restored into _sessions
        assert mock_mgr._sessions.get(3328) == "saved-session-xyz"


# ── handle_message tests ──────────────────────────────────────────────


class TestAgentHandleMessage:
    """Test the agent bot's handle_message entry point."""

    async def test_unauthorized_rejected(self):
        from bot_agent import handle_message

        update = _make_update()
        ctx = _make_context()
        with patch("bot_agent.is_authorized", return_value=False):
            await handle_message(update, ctx)
        update.message.reply_text.assert_called_once()
        assert "not authorized" in update.message.reply_text.call_args[0][0]

    async def test_empty_message_skipped(self):
        from bot_agent import handle_message

        update = _make_update(text="")
        update.message.text = None
        update.message.caption = None
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent._run_cli", new_callable=AsyncMock) as mock_run,
        ):
            await handle_message(update, ctx)
        mock_run.assert_not_called()

    async def test_text_message_processed(self):
        from bot_agent import handle_message

        update = _make_update(text="do something")
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent._run_cli", new_callable=AsyncMock) as mock_run,
            patch("bot_agent.audit_log"),
        ):
            await handle_message(update, ctx)
        mock_run.assert_called_once()

    async def test_no_message_noop(self):
        from bot_agent import handle_message

        update = MagicMock()
        update.message = None
        ctx = _make_context()
        await handle_message(update, ctx)  # should not raise

    async def test_voice_unsupported(self):
        from bot_agent import handle_message

        update = _make_update(text="")
        update.message.text = None
        update.message.caption = None
        update.message.voice = MagicMock()
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent._run_cli", new_callable=AsyncMock) as mock_run,
            patch("bot_agent.audit_log"),
        ):
            await handle_message(update, ctx)
        # Voice adds text about unsupported, then _run_cli is called
        mock_run.assert_called_once()
        prompt = mock_run.call_args[0][1]
        assert "not supported" in prompt.lower()


# ── Agent command handlers ────────────────────────────────────────────


class TestAgentCommands:
    async def test_start_authorized(self):
        from bot_agent import start

        update = _make_update()
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_active_repo", return_value=None),
        ):
            await start(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Agent" in text

    async def test_new_conversation_clears_session(self):
        from bot_agent import new_conversation

        update = _make_update(chat_id=3320)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.clear_conversation"),
            patch("bot_agent.save_active_branch"),
            patch("bot_agent.get_active_repo", return_value=None),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.save_session_id") as mock_save_session,
        ):
            mock_mgr.new_session = MagicMock()
            mock_mgr.abort = AsyncMock(return_value=False)
            await new_conversation(update, ctx)
        mock_mgr.new_session.assert_called_once_with(3320)
        mock_mgr.abort.assert_called_once_with(3320)
        mock_save_session.assert_called_once_with(3320, None)
        text = update.message.reply_text.call_args[0][0]
        assert "cleared" in text.lower()

    async def test_show_model_no_args(self):
        from bot_agent import show_model

        update = _make_update()
        ctx = _make_context(args=[])
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_model", return_value="claude-opus-4-6"),
        ):
            await show_model(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "opus" in text.lower()

    async def test_show_version(self):
        from bot_agent import show_version

        update = _make_update()
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_model", return_value="claude-test"),
        ):
            await show_version(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Agent" in text


# ── keep_typing tests ─────────────────────────────────────────────────


class TestAgentKeepTyping:
    async def test_stops_on_event(self):
        import asyncio

        from bot_agent import keep_typing

        chat = AsyncMock()
        bot = AsyncMock()
        stop = asyncio.Event()
        stop.set()
        await keep_typing(chat, stop, bot)
        # Should complete quickly
