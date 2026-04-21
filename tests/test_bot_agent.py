"""Tests for bot_agent.py — command handlers and message handling."""

from unittest.mock import AsyncMock, MagicMock, patch


class TestFormatProgress:
    """Test _format_tool_progress() formatting for both text and tool_use blocks."""

    def test_read_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "Read", "input": {"file_path": "/app/src/main.py"}})
        assert result == "Reading app/src/main.py"

    def test_write_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress(
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/app/src/main.py"}}
        )
        assert result == "Writing app/src/main.py"

    def test_edit_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b/c.py"}})
        assert result == "Editing a/b/c.py"

    def test_bash_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}})
        assert result == "$ npm test"

    def test_bash_multiline_truncated(self):
        from bot_agent import _format_tool_progress

        long_cmd = "a" * 100 + "\nsecond line"
        result = _format_tool_progress({"type": "tool_use", "name": "Bash", "input": {"command": long_cmd}})
        assert result == f"$ {long_cmd}"

    def test_glob_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "Glob", "input": {"pattern": "**/*.py"}})
        assert result == "Finding **/*.py"

    def test_grep_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}})
        assert result == "Searching: TODO"

    def test_task_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "Task", "input": {"description": "explore code"}})
        assert result == "Subagent: explore code"

    def test_unknown_tool_fallback(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "web_fetch", "input": {}})
        assert result == "Web Fetch"

    def test_empty_name(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "", "input": {}})
        assert result is None

    def test_empty_input(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "tool_use", "name": "Read", "input": {}})
        assert result is None

    # ── Text block (reasoning) tests ──

    def test_text_block_short(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "text", "text": "Looking at the auth module"})
        assert result == "Looking at the auth module"

    def test_text_block_empty(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "text", "text": ""})
        assert result is None

    def test_text_block_whitespace_only(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"type": "text", "text": "   \n  "})
        assert result is None

    def test_text_block_not_truncated(self):
        from bot_agent import _format_tool_progress

        long_text = "x" * 400
        result = _format_tool_progress({"type": "text", "text": long_text})
        assert result == long_text


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

from helpers import make_context as _make_context
from helpers import make_update as _make_update

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
            mock_mgr.was_text_streamed.return_value = False
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
            mock_mgr.was_text_streamed.return_value = False
            await _run_cli(3331, "do something", update, ctx)
        sent = mock_send.call_args[0][1]
        assert "no output" in sent.lower()

    async def test_streamed_text_not_double_sent(self):
        from bot_agent import _run_cli

        update = _make_update(chat_id=3335)
        ctx = _make_context()
        with (
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_model", return_value="claude-test"),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send,
            patch("bot_agent.save_conversation") as mock_save,
            patch("bot_agent.load_conversation", return_value=[]),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.load_session_id", return_value=None),
        ):
            mock_mgr.run = AsyncMock(return_value="Already streamed text")
            mock_mgr.was_text_streamed.return_value = True
            await _run_cli(3335, "do something", update, ctx)
        mock_send.assert_not_called()
        mock_save.assert_called_once()
        saved_history = mock_save.call_args[0][1]
        assert saved_history[-1]["content"] == "Already streamed text"

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
            mock_mgr.was_text_streamed.return_value = False
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
            mock_mgr.was_text_streamed.return_value = False
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
            mock_mgr.was_text_streamed.return_value = False
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
            patch("bot_agent.get_model", return_value="opus"),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.save_session_id") as mock_save_session,
            patch("bot_agent.update_claude_cli", new_callable=AsyncMock, return_value=(True, "Updated")),
        ):
            mock_mgr.new_session = MagicMock()
            mock_mgr.abort = AsyncMock(return_value=False)
            await new_conversation(update, ctx)
        mock_mgr.new_session.assert_called_once_with(3320)
        mock_mgr.abort.assert_called_once_with(3320)
        mock_save_session.assert_called_once_with(3320, None)
        first_reply = update.message.reply_text.call_args_list[0][0][0]
        assert "cleared" in first_reply.lower()

    async def test_show_model_no_args(self):
        from bot_agent import show_model

        update = _make_update()
        ctx = _make_context(args=[])
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_model", return_value="opus"),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.get_last_model = MagicMock(return_value="claude-opus-4-7")
            await show_model(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "opus" in text.lower()
        assert "claude-opus-4-7" in text

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


# ── Plan / Work mode tests ────────────────────────────────────────────


class TestPlanWorkMode:
    async def test_plan_no_args_enables_plan_mode(self):
        import bot_agent
        from bot_agent import plan_command

        update = _make_update(chat_id=5001)
        update.message.text = "/plan"
        ctx = _make_context()
        with patch("bot_agent.is_authorized", return_value=True):
            await plan_command(update, ctx)
        assert 5001 in bot_agent._plan_mode
        text = update.message.reply_text.call_args[0][0]
        assert "plan mode on" in text.lower()
        # Clean up
        bot_agent._plan_mode.discard(5001)

    async def test_plan_with_task_runs_cli(self):
        from bot_agent import plan_command

        update = _make_update(chat_id=5002)
        update.message.text = "/plan implement auth"
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent._run_cli", new_callable=AsyncMock) as mock_run,
        ):
            await plan_command(update, ctx)
        mock_run.assert_called_once()
        prompt = mock_run.call_args[0][1]
        assert "implement auth" in prompt

    async def test_work_disables_plan_mode(self):
        import bot_agent
        from bot_agent import work_command

        bot_agent._plan_mode.add(5003)
        update = _make_update(chat_id=5003)
        ctx = _make_context()
        with patch("bot_agent.is_authorized", return_value=True):
            await work_command(update, ctx)
        assert 5003 not in bot_agent._plan_mode
        text = update.message.reply_text.call_args[0][0]
        assert "plan mode off" in text.lower()

    async def test_work_when_not_in_plan_mode(self):
        import bot_agent
        from bot_agent import work_command

        bot_agent._plan_mode.discard(5004)
        update = _make_update(chat_id=5004)
        ctx = _make_context()
        with patch("bot_agent.is_authorized", return_value=True):
            await work_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "already" in text.lower()


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


# ── Stream mode (/newstream) tests ────────────────────────────────────


class TestNewStream:
    """Tests for /newstream command and stream-mode routing in handle_message."""

    async def test_new_stream_requires_repo(self):
        from bot_agent import new_stream

        update = _make_update(chat_id=6001)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_active_repo", return_value=None),
        ):
            await new_stream(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No repo set" in text

    async def test_new_stream_starts_and_sets_flag(self):
        import bot_agent
        from bot_agent import new_stream

        update = _make_update(chat_id=6002)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.get_model", return_value="opus"),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.stop_stream = AsyncMock()
            mock_mgr.abort = AsyncMock(return_value=False)
            mock_mgr.new_session = MagicMock()
            mock_mgr.start_stream = AsyncMock()
            bot_agent._stream_mode.discard(6002)
            await new_stream(update, ctx)
        assert 6002 in bot_agent._stream_mode
        mock_mgr.start_stream.assert_awaited_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "Stream mode ON" in reply
        bot_agent._stream_mode.discard(6002)  # cleanup

    async def test_new_stream_failure_does_not_set_flag(self):
        import bot_agent
        from bot_agent import new_stream

        update = _make_update(chat_id=6003)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_active_repo", return_value="owner/repo"),
            patch("bot_agent.get_active_branch", return_value=None),
            patch("bot_agent.get_model", return_value="opus"),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.stop_stream = AsyncMock()
            mock_mgr.abort = AsyncMock(return_value=False)
            mock_mgr.new_session = MagicMock()
            mock_mgr.start_stream = AsyncMock(side_effect=RuntimeError("launch failed"))
            bot_agent._stream_mode.discard(6003)
            await new_stream(update, ctx)
        assert 6003 not in bot_agent._stream_mode
        # Error reply sent
        replies = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("Failed to start stream" in r for r in replies)

    async def test_handle_message_in_stream_mode_feeds_stdin(self):
        import bot_agent
        from bot_agent import handle_message

        update = _make_update(chat_id=6004, text="hello claude")
        ctx = _make_context()
        bot_agent._stream_mode.add(6004)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                patch("bot_agent._run_cli", new_callable=AsyncMock) as mock_run_cli,
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr.is_processing = MagicMock(return_value=False)
                mock_mgr.stream_mode_active = MagicMock(return_value=True)
                mock_mgr.feed = AsyncMock(return_value=True)
                await handle_message(update, ctx)
            mock_mgr.feed.assert_awaited_once_with(6004, "hello claude")
            mock_run_cli.assert_not_called()
        finally:
            bot_agent._stream_mode.discard(6004)

    async def test_handle_message_stream_inactive_falls_back(self):
        """If _stream_mode flag is set but reader task died, drop flag and run one-shot."""
        import bot_agent
        from bot_agent import handle_message

        update = _make_update(chat_id=6005, text="hi")
        ctx = _make_context()
        bot_agent._stream_mode.add(6005)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                patch("bot_agent._run_cli", new_callable=AsyncMock) as mock_run_cli,
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr.is_processing = MagicMock(return_value=False)
                mock_mgr.stream_mode_active = MagicMock(return_value=False)
                mock_mgr.feed = AsyncMock()
                await handle_message(update, ctx)
            assert 6005 not in bot_agent._stream_mode
            mock_mgr.feed.assert_not_called()
            mock_run_cli.assert_called_once()
        finally:
            bot_agent._stream_mode.discard(6005)

    async def test_stop_work_clears_stream_mode_flag(self):
        import bot_agent
        from bot_agent import stop_work

        update = _make_update(chat_id=6006)
        ctx = _make_context()
        bot_agent._stream_mode.add(6006)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr.abort = AsyncMock(return_value=False)
                await stop_work(update, ctx)
            assert 6006 not in bot_agent._stream_mode
            text = update.message.reply_text.call_args[0][0]
            assert "Stream stopped" in text
        finally:
            bot_agent._stream_mode.discard(6006)

    async def test_new_conversation_tears_down_stream(self):
        import bot_agent
        from bot_agent import new_conversation

        update = _make_update(chat_id=6007)
        ctx = _make_context()
        bot_agent._stream_mode.add(6007)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.clear_conversation"),
                patch("bot_agent.save_active_branch"),
                patch("bot_agent.get_active_repo", return_value=None),
                patch("bot_agent.get_active_branch", return_value=None),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.save_session_id"),
                patch("bot_agent.update_claude_cli", new_callable=AsyncMock, return_value=(True, "Updated")),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr.new_session = MagicMock()
                mock_mgr.abort = AsyncMock(return_value=False)
                mock_mgr.stop_stream = AsyncMock()
                await new_conversation(update, ctx)
            assert 6007 not in bot_agent._stream_mode
            mock_mgr.stop_stream.assert_awaited_once_with(6007, kill_proc=True)
        finally:
            bot_agent._stream_mode.discard(6007)


class TestStreamEventHandler:
    """Test the on_event callback built by _make_stream_event_handler."""

    async def test_assistant_text_posts_to_chat(self):
        from bot_agent import _make_stream_event_handler

        bot = AsyncMock()
        with patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send:
            handler = _make_stream_event_handler(7001, bot)
            await handler({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        mock_send.assert_awaited()
        # chat_id argument passed through
        assert mock_send.call_args[0][0] == 7001

    async def test_tool_use_block_posts_formatted_line(self):
        from bot_agent import _make_stream_event_handler

        bot = AsyncMock()
        with patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send:
            handler = _make_stream_event_handler(7002, bot)
            await handler(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/y/z.py"}}]},
                }
            )
        mock_send.assert_awaited()
        text = mock_send.call_args[0][1]
        assert "Reading" in text

    async def test_stream_end_clears_mode(self):
        import bot_agent
        from bot_agent import _make_stream_event_handler

        bot_agent._stream_mode.add(7003)
        try:
            bot = AsyncMock()
            with patch("bot_agent.send_long_message", new_callable=AsyncMock):
                handler = _make_stream_event_handler(7003, bot)
                await handler({"_type": "stream_end", "reason": "eof"})
            assert 7003 not in bot_agent._stream_mode
        finally:
            bot_agent._stream_mode.discard(7003)

    async def test_init_system_event_ignored(self):
        """init events should NOT post to chat (they're already used for model tracking)."""
        from bot_agent import _make_stream_event_handler

        bot = AsyncMock()
        with patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send:
            handler = _make_stream_event_handler(7004, bot)
            await handler({"type": "system", "subtype": "init", "model": "claude-opus-4-7"})
        mock_send.assert_not_called()


class TestCancelCommand:
    """Tests for /cancel — soft interrupt that keeps the session alive."""

    async def test_cancel_no_running_proc(self):
        from bot_agent import cancel_work

        update = _make_update(chat_id=8001)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.has_running_proc = MagicMock(return_value=False)
            mock_mgr.interrupt = AsyncMock()
            await cancel_work(update, ctx)
        mock_mgr.interrupt.assert_not_called()
        text = update.message.reply_text.call_args[0][0]
        assert "nothing" in text.lower()

    async def test_cancel_sends_interrupt(self):
        from bot_agent import cancel_work

        update = _make_update(chat_id=8002)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.has_running_proc = MagicMock(return_value=True)
            mock_mgr.interrupt = AsyncMock(return_value=True)
            await cancel_work(update, ctx)
        mock_mgr.interrupt.assert_awaited_once_with(8002)
        text = update.message.reply_text.call_args[0][0]
        assert "interrupt" in text.lower()
        assert "preserved" in text.lower()

    async def test_cancel_write_failure(self):
        from bot_agent import cancel_work

        update = _make_update(chat_id=8003)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.has_running_proc = MagicMock(return_value=True)
            mock_mgr.interrupt = AsyncMock(return_value=False)
            await cancel_work(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "couldn't" in text.lower() or "stop" in text.lower()

    async def test_cancel_unauthorized(self):
        from bot_agent import cancel_work

        update = _make_update()
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=False),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.interrupt = AsyncMock()
            await cancel_work(update, ctx)
        mock_mgr.interrupt.assert_not_called()


class TestRepoSwitchInStreamMode:
    """Tests for /repo switching while /newstream is active."""

    async def test_repo_switch_tears_down_stream(self):
        import bot_agent
        from bot_agent import set_repo

        update = _make_update(chat_id=9001)
        ctx = _make_context(args=["owner/new-repo"])
        bot_agent._stream_mode.add(9001)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.save_active_repo"),
                patch("bot_agent.save_session_id"),
                patch("bot_agent.set_active_branch"),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
                patch("asyncio.create_task") as mock_create_task,
            ):
                mock_mgr.stop_stream = AsyncMock()
                mock_mgr.new_session = MagicMock()
                # capture the clone-notify coroutine so we can drive it
                mock_create_task.side_effect = lambda coro: coro.close() or MagicMock()
                await set_repo(update, ctx)
            # Stream was torn down immediately (before clone starts)
            mock_mgr.stop_stream.assert_awaited_once_with(9001, kill_proc=True)
            assert 9001 not in bot_agent._stream_mode
        finally:
            bot_agent._stream_mode.discard(9001)

    async def test_repo_switch_not_in_stream_mode_does_not_call_stop_stream(self):
        import bot_agent
        from bot_agent import set_repo

        update = _make_update(chat_id=9002)
        ctx = _make_context(args=["owner/new-repo"])
        bot_agent._stream_mode.discard(9002)
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.save_active_repo"),
            patch("bot_agent.save_session_id"),
            patch("bot_agent.set_active_branch"),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("asyncio.create_task") as mock_create_task,
        ):
            mock_mgr.stop_stream = AsyncMock()
            mock_mgr.new_session = MagicMock()
            mock_create_task.side_effect = lambda coro: coro.close() or MagicMock()
            await set_repo(update, ctx)
        mock_mgr.stop_stream.assert_not_called()

    async def test_repo_switch_restarts_stream_after_clone(self):
        """After a successful clone in stream-mode switch, stream is relaunched."""
        import bot_agent
        from bot_agent import set_repo

        update = _make_update(chat_id=9003)
        ctx = _make_context(args=["owner/new-repo"])
        bot_agent._stream_mode.add(9003)

        captured_coros: list = []

        def _capture(coro):
            captured_coros.append(coro)
            return MagicMock()

        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.save_active_repo"),
                patch("bot_agent.save_session_id"),
                patch("bot_agent.set_active_branch"),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
                patch("asyncio.create_task", side_effect=_capture),
            ):
                mock_mgr.stop_stream = AsyncMock()
                mock_mgr.new_session = MagicMock()
                mock_mgr.ensure_clone = AsyncMock()
                mock_mgr.start_stream = AsyncMock()
                await set_repo(update, ctx)

                # Drive the captured clone-notify coroutine
                assert len(captured_coros) == 1
                await captured_coros[0]

            mock_mgr.start_stream.assert_awaited_once()
            call_kwargs = mock_mgr.start_stream.call_args.kwargs
            assert call_kwargs["chat_id"] == 9003
            assert call_kwargs["repo"] == "owner/new-repo"
            # Stream flag is re-added after successful restart
            assert 9003 in bot_agent._stream_mode
        finally:
            bot_agent._stream_mode.discard(9003)
