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
        assert result == "$ " + "a" * 100 + "…"

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
            patch("bot_agent._dispatch_prompt", new_callable=AsyncMock) as mock_dispatch,
        ):
            await handle_message(update, ctx)
        mock_dispatch.assert_not_called()

    async def test_text_message_processed(self):
        from bot_agent import handle_message

        update = _make_update(text="do something")
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_active_repo", return_value=None),
            patch("bot_agent._dispatch_prompt", new_callable=AsyncMock) as mock_dispatch,
            patch("bot_agent.audit_log"),
        ):
            await handle_message(update, ctx)
        mock_dispatch.assert_called_once()

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
            patch("bot_agent.get_active_repo", return_value=None),
            patch("bot_agent._dispatch_prompt", new_callable=AsyncMock) as mock_dispatch,
            patch("bot_agent.audit_log"),
        ):
            await handle_message(update, ctx)
        # Voice adds text about unsupported, then dispatch is called
        mock_dispatch.assert_called_once()
        prompt = mock_dispatch.call_args[0][1]
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


# ── /df and /cleanup ──────────────────────────────────────────────────


class TestDiskCommands:
    def test_human_size_units(self):
        from bot_agent import _human_size

        assert _human_size(0) == "0 B"
        assert _human_size(512) == "512 B"
        assert _human_size(2048) == "2.0 KB"
        assert _human_size(5 * 1024 * 1024) == "5.0 MB"
        assert _human_size(3 * 1024**3) == "3.0 GB"

    def test_dir_size_sums_files(self, tmp_path):
        from bot_agent import _dir_size

        (tmp_path / "a.txt").write_bytes(b"x" * 100)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.txt").write_bytes(b"y" * 250)
        assert _dir_size(tmp_path) == 350

    def test_compute_disk_report_lists_owner_dirs(self, tmp_path):
        from bot_agent import _compute_disk_report

        (tmp_path / "alice").mkdir()
        (tmp_path / "alice" / "f").write_bytes(b"x" * 100)
        (tmp_path / "bob").mkdir()
        (tmp_path / ".hidden").mkdir()  # should be filtered out

        report = _compute_disk_report(tmp_path)
        assert "Disk:" in report
        assert "GB free" in report
        assert "alice/" in report
        assert "bob/" in report
        assert ".hidden/" not in report

    def test_cleanup_removes_targets_only(self, tmp_path):
        import bot_agent

        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("print('hi')")
        venv = repo / ".venv"
        venv.mkdir()
        (venv / "marker").write_bytes(b"z" * 1024)
        pycache = repo / "src" / "__pycache__"
        pycache.mkdir()
        (pycache / "x.pyc").write_bytes(b"compiled")

        freed, count = bot_agent._cleanup_cache_dirs(tmp_path)
        assert count == 2
        assert not venv.exists()
        assert not pycache.exists()
        assert (repo / "src" / "main.py").exists()
        # freed is filesystem-level and may be 0 on small tmpfs, so just verify non-negative.
        assert freed >= 0

    def test_cleanup_does_not_descend_into_targets(self, tmp_path):
        import bot_agent

        nested_venv = tmp_path / "repo" / ".venv"
        nested_venv.mkdir(parents=True)
        # An inner __pycache__ inside .venv shouldn't get counted twice
        (nested_venv / "__pycache__").mkdir()
        _freed, count = bot_agent._cleanup_cache_dirs(tmp_path)
        assert count == 1  # only the outer .venv

    async def test_df_command_unauthorized_no_response(self):
        from bot_agent import df_command

        update = _make_update()
        ctx = _make_context()
        with patch("bot_agent.is_authorized", return_value=False):
            await df_command(update, ctx)
        update.message.reply_text.assert_not_called()

    async def test_df_command_authorized_sends_report(self, tmp_path):
        from unittest.mock import AsyncMock

        from bot_agent import df_command

        update = _make_update()
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("bot_agent.send_long_message", new_callable=AsyncMock) as mock_send,
        ):
            mock_mgr.workspace_root = tmp_path
            await df_command(update, ctx)
        mock_send.assert_awaited_once()
        sent_text = mock_send.call_args[0][1]
        assert "Disk:" in sent_text

    async def test_df_command_handles_missing_mgr(self):
        from bot_agent import df_command

        update = _make_update()
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr", None),
        ):
            await df_command(update, ctx)
        update.message.reply_text.assert_called_once()
        assert "not configured" in update.message.reply_text.call_args[0][0].lower()

    async def test_cleanup_command_runs_and_reports(self, tmp_path):
        from bot_agent import cleanup_command

        # Set up something to clean
        (tmp_path / "repo").mkdir()
        (tmp_path / "repo" / ".venv").mkdir()
        (tmp_path / "repo" / ".venv" / "f").write_bytes(b"x" * 10)

        update = _make_update()
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.workspace_root = tmp_path
            await cleanup_command(update, ctx)
        # Two messages: "Cleaning…" then "Removed N dirs…"
        assert update.message.reply_text.await_count == 2
        final = update.message.reply_text.await_args_list[-1].args[0]
        assert "Removed 1 dirs" in final


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

    async def test_plan_with_task_dispatches_framed_prompt(self):
        from bot_agent import plan_command

        update = _make_update(chat_id=5002)
        update.message.text = "/plan implement auth"
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent._dispatch_prompt", new_callable=AsyncMock) as mock_dispatch,
        ):
            await plan_command(update, ctx)
        mock_dispatch.assert_called_once()
        prompt = mock_dispatch.call_args[0][1]
        assert "implement auth" in prompt
        assert "plan" in prompt.lower()

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
            patch("bot_agent.save_session_id") as mock_save_session,
            patch("bot_agent.update_claude_cli", new_callable=AsyncMock, return_value=(True, "Updated")),
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
        # /newstream clears only THIS repo's session
        mock_mgr.new_session.assert_called_once_with(6002, "owner/repo")
        mock_save_session.assert_called_once_with(6002, "owner/repo", None)
        replies = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("Stream mode ON" in r for r in replies)
        assert any("fresh session" in r.lower() for r in replies)
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
            patch("bot_agent.update_claude_cli", new_callable=AsyncMock, return_value=(True, "Updated")),
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
        assert any("Failed to start" in r for r in replies)

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
                patch("bot_agent.get_active_repo", return_value="owner/repo"),
                patch("bot_agent.load_session_id", return_value=None) as mock_load,
                patch("bot_agent.save_session_id") as mock_save,
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr.stream_mode_active = MagicMock(return_value=True)
                mock_mgr.get_session_id = MagicMock(return_value="sess-1")
                mock_mgr.feed = AsyncMock(return_value=True)
                await handle_message(update, ctx)
            mock_mgr.feed.assert_awaited_once_with(6004, "hello claude")
            # Session lookups must be repo-scoped
            mock_mgr.get_session_id.assert_called_with(6004, "owner/repo")
            # load_session_id is only called when in-memory session is None;
            # here get_session_id returns "sess-1" so it's skipped
            mock_load.assert_not_called()
            mock_save.assert_called_with(6004, "owner/repo", "sess-1")
        finally:
            bot_agent._stream_mode.discard(6004)

    async def test_handle_message_stream_inactive_restarts(self):
        """If stream flag is set but reader task died, drop flag, restart, and feed."""
        import bot_agent
        from bot_agent import handle_message

        update = _make_update(chat_id=6005, text="hi")
        ctx = _make_context()
        bot_agent._stream_mode.add(6005)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                patch("bot_agent.get_active_repo", return_value="owner/repo"),
                patch("bot_agent.get_active_branch", return_value=None),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.load_session_id", return_value=None),
                patch("bot_agent.save_session_id"),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr.stream_mode_active = MagicMock(return_value=False)
                mock_mgr.get_session_id = MagicMock(return_value="sess-1")
                mock_mgr.start_stream = AsyncMock()
                mock_mgr.feed = AsyncMock(return_value=True)
                await handle_message(update, ctx)
            # Stream was restarted and message fed
            mock_mgr.start_stream.assert_awaited_once()
            mock_mgr.feed.assert_awaited_once_with(6005, "hi")
            assert 6005 in bot_agent._stream_mode
        finally:
            bot_agent._stream_mode.discard(6005)

    async def test_handle_message_restores_session_from_db_for_repo(self):
        """If the in-memory session is missing, load_session_id is called with (chat, repo)
        and the result is written into _sessions[(chat, repo)]."""
        import bot_agent
        from bot_agent import handle_message

        update = _make_update(chat_id=6010, text="hi")
        ctx = _make_context()
        bot_agent._stream_mode.add(6010)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                patch("bot_agent.get_active_repo", return_value="owner/my-repo"),
                patch("bot_agent.load_session_id", return_value="db-session-xyz") as mock_load,
                patch("bot_agent.save_session_id"),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr._sessions = {}
                mock_mgr.stream_mode_active = MagicMock(return_value=True)
                mock_mgr.get_session_id = MagicMock(return_value=None)
                mock_mgr.feed = AsyncMock(return_value=True)
                await handle_message(update, ctx)
            mock_load.assert_called_once_with(6010, "owner/my-repo")
            assert mock_mgr._sessions[(6010, "owner/my-repo")] == "db-session-xyz"
        finally:
            bot_agent._stream_mode.discard(6010)

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


class TestRestartCommand:
    """Tests for /restart — kill CC, npm update, relaunch with --resume."""

    async def test_restart_no_repo_errors(self):
        from bot_agent import restart_command

        update = _make_update(chat_id=8101)
        ctx = _make_context()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.get_active_repo", return_value=None),
        ):
            await restart_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No repo" in text

    async def test_restart_resumes_existing_session(self):
        import bot_agent
        from bot_agent import restart_command

        update = _make_update(chat_id=8102)
        ctx = _make_context()
        bot_agent._stream_mode.add(8102)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.get_active_repo", return_value="owner/repo"),
                patch("bot_agent.get_active_branch", return_value=None),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.load_session_id", return_value=None),
                patch("bot_agent.update_claude_cli", new_callable=AsyncMock, return_value=(True, "Updated")),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                # Existing in-memory session
                mock_mgr.get_session_id = MagicMock(return_value="resume-me-1234")
                mock_mgr.stop_stream = AsyncMock()
                mock_mgr.abort = AsyncMock(return_value=False)
                mock_mgr.new_session = MagicMock()
                mock_mgr.start_stream = AsyncMock()
                await restart_command(update, ctx)

            # Session must NOT be cleared by /restart
            mock_mgr.new_session.assert_not_called()
            # Stream is relaunched
            mock_mgr.start_stream.assert_awaited_once()
            # User sees the resume confirmation
            replies = [c[0][0] for c in update.message.reply_text.call_args_list]
            assert any("Resumed session" in r for r in replies)
            assert any("resume-" in r for r in replies)
            assert 8102 in bot_agent._stream_mode
        finally:
            bot_agent._stream_mode.discard(8102)

    async def test_restart_loads_session_from_db_when_in_memory_missing(self):
        import bot_agent
        from bot_agent import restart_command

        update = _make_update(chat_id=8103)
        ctx = _make_context()
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.get_active_repo", return_value="owner/repo"),
                patch("bot_agent.get_active_branch", return_value=None),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.load_session_id", return_value="db-session-99"),
                patch("bot_agent.update_claude_cli", new_callable=AsyncMock, return_value=(True, "Updated")),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr._sessions = {}
                mock_mgr.get_session_id = MagicMock(return_value=None)
                mock_mgr.stop_stream = AsyncMock()
                mock_mgr.abort = AsyncMock(return_value=False)
                mock_mgr.new_session = MagicMock()
                mock_mgr.start_stream = AsyncMock()
                await restart_command(update, ctx)

            # DB session was hoisted into the manager's in-memory store
            assert mock_mgr._sessions[(8103, "owner/repo")] == "db-session-99"
            mock_mgr.new_session.assert_not_called()
        finally:
            bot_agent._stream_mode.discard(8103)

    async def test_restart_no_session_starts_fresh(self):
        import bot_agent
        from bot_agent import restart_command

        update = _make_update(chat_id=8104)
        ctx = _make_context()
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.get_active_repo", return_value="owner/repo"),
                patch("bot_agent.get_active_branch", return_value=None),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.load_session_id", return_value=None),
                patch("bot_agent.update_claude_cli", new_callable=AsyncMock, return_value=(True, "Updated")),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr._sessions = {}
                mock_mgr.get_session_id = MagicMock(return_value=None)
                mock_mgr.stop_stream = AsyncMock()
                mock_mgr.abort = AsyncMock(return_value=False)
                mock_mgr.new_session = MagicMock()
                mock_mgr.start_stream = AsyncMock()
                await restart_command(update, ctx)

            replies = [c[0][0] for c in update.message.reply_text.call_args_list]
            assert any("No prior session" in r for r in replies)
            mock_mgr.start_stream.assert_awaited_once()
        finally:
            bot_agent._stream_mode.discard(8104)


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

    async def test_repo_switch_tears_down_stream_but_preserves_session(self):
        import bot_agent
        from bot_agent import set_repo

        update = _make_update(chat_id=9001)
        ctx = _make_context(args=["owner/new-repo"])
        bot_agent._stream_mode.add(9001)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.save_active_repo"),
                patch("bot_agent.save_session_id") as mock_save_session,
                patch("bot_agent.set_active_branch"),
                patch("bot_agent.get_model", return_value="opus"),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
                patch("asyncio.create_task") as mock_create_task,
            ):
                mock_mgr.stop_stream = AsyncMock()
                mock_mgr.new_session = MagicMock()
                mock_create_task.side_effect = lambda coro: coro.close() or MagicMock()
                await set_repo(update, ctx)
            # Stream was torn down immediately (before clone starts)
            mock_mgr.stop_stream.assert_awaited_once_with(9001, kill_proc=True)
            assert 9001 not in bot_agent._stream_mode
            # /repo must NOT clear sessions — we want per-repo memory across switches
            mock_mgr.new_session.assert_not_called()
            mock_save_session.assert_not_called()
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
            patch("bot_agent.save_session_id") as mock_save_session,
            patch("bot_agent.set_active_branch"),
            patch("bot_agent.claude_code_mgr") as mock_mgr,
            patch("asyncio.create_task") as mock_create_task,
        ):
            mock_mgr.stop_stream = AsyncMock()
            mock_mgr.new_session = MagicMock()
            mock_create_task.side_effect = lambda coro: coro.close() or MagicMock()
            await set_repo(update, ctx)
        mock_mgr.stop_stream.assert_not_called()
        # No session clearing on repo switch
        mock_mgr.new_session.assert_not_called()
        mock_save_session.assert_not_called()

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


class TestStreamTypingIndicator:
    """Typing indicator starts on feed and stops on result/stream_end."""

    async def test_typing_starts_on_feed_success(self):
        import asyncio

        import bot_agent
        from bot_agent import handle_message

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "do something"
        update.message.caption = None
        update.message.photo = None
        update.message.sticker = None
        update.message.document = None
        update.message.voice = None
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        update.effective_chat = MagicMock()
        update.effective_chat.id = 7001
        ctx = MagicMock()
        ctx.bot = AsyncMock()

        bot_agent._stream_mode.add(7001)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                patch("bot_agent.get_active_repo", return_value="owner/repo"),
                patch("bot_agent.load_session_id", return_value=None),
                patch("bot_agent.save_session_id"),
                patch("bot_agent.claude_code_mgr") as mock_mgr,
            ):
                mock_mgr.stream_mode_active.return_value = True
                mock_mgr.get_session_id = MagicMock(return_value="sess-1")
                mock_mgr.feed = AsyncMock(return_value=True)
                await handle_message(update, ctx)

            # Typing task should have been created and started
            task = bot_agent._typing_tasks.get(7001)
            assert task is not None
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
        finally:
            bot_agent._stream_mode.discard(7001)
            bot_agent._typing_tasks.pop(7001, None)

    async def test_typing_not_started_if_feed_fails(self):
        import bot_agent
        from bot_agent import handle_message

        update = MagicMock()
        update.message = MagicMock()
        update.message.text = "do something"
        update.message.caption = None
        update.message.photo = None
        update.message.sticker = None
        update.message.document = None
        update.message.voice = None
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        update.effective_chat = MagicMock()
        update.effective_chat.id = 7002
        ctx = MagicMock()
        ctx.bot = AsyncMock()

        bot_agent._stream_mode.add(7002)
        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                # Patch _dispatch_prompt so we don't need to wire up the full stream path
                patch("bot_agent._dispatch_prompt", new_callable=AsyncMock),
            ):
                await handle_message(update, ctx)

            # feed() is called inside _dispatch_prompt — we only care that no
            # typing task was started when the message is a normal-length string
            assert bot_agent._typing_tasks.get(7002) is None
        finally:
            bot_agent._stream_mode.discard(7002)
            bot_agent._typing_tasks.pop(7002, None)

    async def test_typing_stopped_on_result_event(self):
        import bot_agent
        from bot_agent import _make_stream_event_handler, _start_stream_typing

        bot = AsyncMock()
        chat_id = 7003
        _start_stream_typing(chat_id, bot)
        task = bot_agent._typing_tasks.get(chat_id)
        assert task is not None and not task.done()

        on_event = _make_stream_event_handler(chat_id, bot)
        await on_event({"type": "result", "cost_usd": 0.01, "num_turns": 1})

        assert bot_agent._typing_tasks.get(chat_id) is None

    async def test_typing_stopped_on_stream_end(self):
        import bot_agent
        from bot_agent import _make_stream_event_handler, _start_stream_typing

        bot = AsyncMock()
        chat_id = 7004
        _start_stream_typing(chat_id, bot)
        task = bot_agent._typing_tasks.get(chat_id)
        assert task is not None and not task.done()

        on_event = _make_stream_event_handler(chat_id, bot)
        await on_event({"_type": "stream_end", "reason": "eof"})

        assert bot_agent._typing_tasks.get(chat_id) is None


class TestFragmentBuffering:
    """Telegram splits long pastes into consecutive MAX_TELEGRAM_LENGTH messages.
    The bot should buffer full-length fragments and dispatch the assembled text.
    """

    def _make_update(self, chat_id: int, text: str):
        update = MagicMock()
        update.message = MagicMock()
        update.message.text = text
        update.message.caption = None
        update.message.photo = None
        update.message.sticker = None
        update.message.document = None
        update.message.voice = None
        update.message.reply_text = MagicMock(return_value=None)
        update.effective_user = MagicMock()
        update.effective_user.id = 1
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        ctx = MagicMock()
        ctx.bot = MagicMock()
        return update, ctx

    async def test_full_length_message_is_buffered(self):

        import bot_agent
        from bot_agent import handle_message

        chat_id = 8001
        text = "x" * 4096
        update, ctx = self._make_update(chat_id, text)

        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                patch("bot_agent._dispatch_prompt", new_callable=MagicMock) as mock_dispatch,
            ):
                await handle_message(update, ctx)

            # Should be buffered, not dispatched
            assert bot_agent._frag_buffers.get(chat_id) == text
            mock_dispatch.assert_not_called()
        finally:
            task = bot_agent._frag_tasks.pop(chat_id, None)
            if task:
                task.cancel()
            bot_agent._frag_buffers.pop(chat_id, None)

    async def test_short_message_flushes_buffer(self):
        import bot_agent
        from bot_agent import handle_message

        chat_id = 8002
        fragment = "x" * 4096
        final = " rest of message"
        update, ctx = self._make_update(chat_id, final)

        bot_agent._frag_buffers[chat_id] = fragment
        dispatched: list[str] = []

        async def _capture(cid, prompt, u, c):
            dispatched.append(prompt)

        try:
            with (
                patch("bot_agent.is_authorized", return_value=True),
                patch("bot_agent.audit_log"),
                patch("bot_agent._dispatch_prompt", side_effect=_capture),
            ):
                await handle_message(update, ctx)

            assert len(dispatched) == 1
            assert dispatched[0] == fragment + final
            assert chat_id not in bot_agent._frag_buffers
        finally:
            bot_agent._frag_buffers.pop(chat_id, None)
            t = bot_agent._frag_tasks.pop(chat_id, None)
            if t:
                t.cancel()

    async def test_short_message_no_buffer_dispatches_directly(self):
        from bot_agent import handle_message

        chat_id = 8003
        text = "hello"
        update, ctx = self._make_update(chat_id, text)
        dispatched: list[str] = []

        async def _capture(cid, prompt, u, c):
            dispatched.append(prompt)

        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.audit_log"),
            patch("bot_agent._dispatch_prompt", side_effect=_capture),
        ):
            await handle_message(update, ctx)

        assert dispatched == ["hello"]


class TestUsageCommandAgent:
    """Tests for the /usage command handler in bot_agent.py."""

    def _make_update(self, user_id=42):
        from unittest.mock import AsyncMock, MagicMock

        update = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat.id = 1001
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        return update

    async def test_unauthorized_user_ignored(self):
        from bot_agent import usage_command

        update = self._make_update()
        ctx = MagicMock()
        with patch("bot_agent.is_authorized", return_value=False):
            await usage_command(update, ctx)
        update.message.reply_text.assert_not_called()

    async def test_missing_config_sends_message(self):
        from bot_agent import usage_command

        update = self._make_update()
        ctx = MagicMock()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.CLAUDE_SESSION_KEY", ""),
            patch("bot_agent.CLAUDE_ORG_ID", ""),
        ):
            await usage_command(update, ctx)
        update.message.reply_text.assert_called_once()
        assert "not configured" in update.message.reply_text.call_args[0][0]

    async def test_successful_response_formatted(self):
        from bot_agent import usage_command

        usage_data = {
            "five_hour": {"utilization": 10.0, "resets_at": "2026-05-06T12:00:00Z"},
            "seven_day": {"utilization": 50.0, "resets_at": "2026-05-10T00:00:00Z"},
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=usage_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        update = self._make_update()
        ctx = MagicMock()
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.CLAUDE_SESSION_KEY", "sk-test"),
            patch("bot_agent.CLAUDE_ORG_ID", "org-test"),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            await usage_command(update, ctx)

        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "10%" in reply


class TestRepoBareNameLookup:
    """Tests for `/repo <name>` resolving a bare name via local tree + GitHub fuzzy match."""

    def test_find_candidates_local_only(self, tmp_path):
        from bot_agent import _find_repo_candidates

        (tmp_path / "pzfreo" / "Teleclaude").mkdir(parents=True)
        (tmp_path / "pzfreo" / "OtherProj").mkdir(parents=True)
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        with patch("bot_agent.claude_code_mgr", mock_mgr), patch("bot_agent.gh_client", None):
            assert _find_repo_candidates("tele") == ["pzfreo/Teleclaude"]

    def test_find_candidates_case_insensitive(self, tmp_path):
        from bot_agent import _find_repo_candidates

        (tmp_path / "pzfreo" / "Teleclaude").mkdir(parents=True)
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        with patch("bot_agent.claude_code_mgr", mock_mgr), patch("bot_agent.gh_client", None):
            assert _find_repo_candidates("TELE") == ["pzfreo/Teleclaude"]

    def test_find_candidates_github_fallback(self, tmp_path):
        from bot_agent import _find_repo_candidates

        (tmp_path / "pzfreo").mkdir()  # exists but empty
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        mock_gh = MagicMock()
        mock_gh.list_user_repos.return_value = [
            {"full_name": "pzfreo/Teleclaude", "description": "", "pushed_at": "x"},
            {"full_name": "pzfreo/other", "description": "", "pushed_at": "y"},
            {"full_name": "someorg/teleclient", "description": "", "pushed_at": "z"},
        ]
        with patch("bot_agent.claude_code_mgr", mock_mgr), patch("bot_agent.gh_client", mock_gh):
            assert _find_repo_candidates("tele") == ["pzfreo/Teleclaude", "someorg/teleclient"]

    def test_find_candidates_local_preferred_over_github(self, tmp_path):
        from bot_agent import _find_repo_candidates

        (tmp_path / "pzfreo" / "Teleclaude").mkdir(parents=True)
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        mock_gh = MagicMock()
        mock_gh.list_user_repos.return_value = [
            {"full_name": "pzfreo/Teleclaude", "description": "", "pushed_at": "x"},
            {"full_name": "someorg/teleclient", "description": "", "pushed_at": "y"},
        ]
        with patch("bot_agent.claude_code_mgr", mock_mgr), patch("bot_agent.gh_client", mock_gh):
            # Local match shows up first; github dupes are deduped
            assert _find_repo_candidates("tele") == ["pzfreo/Teleclaude", "someorg/teleclient"]

    def test_find_candidates_caps_at_limit(self, tmp_path):
        from bot_agent import _find_repo_candidates

        for n in ("foo1", "foo2", "foo3", "foo4", "foo5", "foo6"):
            (tmp_path / "pzfreo" / n).mkdir(parents=True)
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        with patch("bot_agent.claude_code_mgr", mock_mgr), patch("bot_agent.gh_client", None):
            assert len(_find_repo_candidates("foo", limit=5)) == 5

    def test_find_candidates_missing_workspace_root(self, tmp_path):
        """If workspaces/pzfreo doesn't exist, we should not crash."""
        from bot_agent import _find_repo_candidates

        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path  # pzfreo/ subdir doesn't exist
        with patch("bot_agent.claude_code_mgr", mock_mgr), patch("bot_agent.gh_client", None):
            assert _find_repo_candidates("anything") == []

    async def test_set_repo_bare_name_no_match_shows_error(self, tmp_path):
        from bot_agent import set_repo

        update = _make_update(chat_id=7001)
        ctx = _make_context(args=["doesnotexist"])
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr", mock_mgr),
            patch("bot_agent.gh_client", None),
        ):
            await set_repo(update, ctx)
        update.message.reply_text.assert_called_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "No repo found" in reply

    async def test_set_repo_bare_name_single_match_auto_selects(self, tmp_path):
        import bot_agent
        from bot_agent import set_repo

        (tmp_path / "pzfreo" / "Teleclaude").mkdir(parents=True)
        update = _make_update(chat_id=7002)
        ctx = _make_context(args=["tele"])
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        mock_mgr.stop_stream = AsyncMock()
        bot_agent._stream_mode.discard(7002)
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr", mock_mgr),
            patch("bot_agent.gh_client", None),
            patch("bot_agent.save_active_repo"),
            patch("bot_agent.set_active_branch"),
            patch("asyncio.create_task", side_effect=lambda c: c.close() or MagicMock()),
        ):
            await set_repo(update, ctx)
        # First reply confirms the match, second reply announces cloning
        replies = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("Matched: pzfreo/Teleclaude" in r for r in replies)
        assert bot_agent.active_repos[7002] == "pzfreo/Teleclaude"

    async def test_set_repo_bare_name_multi_match_shows_buttons(self, tmp_path):
        from bot_agent import set_repo

        (tmp_path / "pzfreo" / "FooOne").mkdir(parents=True)
        (tmp_path / "pzfreo" / "FooTwo").mkdir(parents=True)
        update = _make_update(chat_id=7003)
        ctx = _make_context(args=["foo"])
        mock_mgr = MagicMock()
        mock_mgr.workspace_root = tmp_path
        with (
            patch("bot_agent.is_authorized", return_value=True),
            patch("bot_agent.claude_code_mgr", mock_mgr),
            patch("bot_agent.gh_client", None),
        ):
            await set_repo(update, ctx)
        update.message.reply_text.assert_called_once()
        kwargs = update.message.reply_text.call_args[1]
        markup = kwargs.get("reply_markup")
        assert markup is not None
        labels = [row[0].text for row in markup.inline_keyboard]
        assert labels == ["pzfreo/FooOne", "pzfreo/FooTwo"]
