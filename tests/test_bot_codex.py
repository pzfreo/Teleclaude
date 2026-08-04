import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from telegram.error import BadRequest

import bot_codex


def _make_update(chat_id: int = 123, user_id: int = 42):
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    return ctx


def _make_callback_update(data: str, chat_id: int = 123, user_id: int = 42):
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.from_user = MagicMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = data
    update.callback_query.message = MagicMock()
    update.callback_query.message.chat_id = chat_id
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


class TestSendPathContainment:
    def test_allows_root_and_child(self, tmp_path):
        root = (tmp_path / "repo").resolve()
        child = (root / "out.txt").resolve()

        assert bot_codex._is_relative_to(root, root) is True
        assert bot_codex._is_relative_to(child, root) is True

    def test_rejects_sibling_with_same_prefix(self, tmp_path):
        root = (tmp_path / "repo").resolve()
        sibling = (tmp_path / "repo2" / "secret.txt").resolve()

        assert bot_codex._is_relative_to(sibling, root) is False

    def test_rejects_tmp_prefix_sibling(self):
        tmp_root = Path("/tmp").resolve()
        tmp_prefix_sibling = Path(str(tmp_root) + "foo").resolve()

        assert bot_codex._is_relative_to(tmp_prefix_sibling, tmp_root) is False


class TestSendFiles:
    async def test_send_file_failure_notifies_user(self, tmp_path):
        path = tmp_path / "report.txt"
        path.write_text("hello")
        bot = MagicMock()
        bot.send_document = AsyncMock(side_effect=BadRequest("file send failed"))
        bot.send_message = AsyncMock()

        sent = await bot_codex._send_file_to_user(123, path, bot)

        assert sent is False
        bot.send_message.assert_awaited_once()
        assert "Failed to send report.txt" in bot.send_message.call_args.kwargs["text"]

    async def test_parse_send_marker_allows_shared_chat_dir(self, tmp_path):
        shared_dir = tmp_path / ".shared" / "123"
        shared_dir.mkdir(parents=True)
        path = shared_dir / "upload.txt"
        path.write_text("hello")
        bot = MagicMock()
        bot.send_document = AsyncMock()

        with patch.object(bot_codex.codex_mgr, "workspace_root", tmp_path):
            remaining = await bot_codex._parse_and_send_markers(123, f"Here [SEND: {path}]", None, bot)

        assert remaining == "Here"
        bot.send_document.assert_awaited_once()

    async def test_parse_markdown_workspace_link_sends_file_and_strips_local_target(self, tmp_path):
        workspace = tmp_path / "pzfreo" / "draftwright"
        path = workspace / "artifacts" / "ctc_review" / "ctc01_sheet.py"
        path.parent.mkdir(parents=True)
        path.write_text("print('hello')\n")
        bot = MagicMock()
        bot.send_document = AsyncMock()

        text = f"Created [{path.name}]({path})"
        with patch.object(bot_codex.codex_mgr, "workspace_path", return_value=workspace):
            remaining = await bot_codex._parse_and_send_markers(123, text, "pzfreo/draftwright", bot)

        assert remaining == f"Created {path.name}"
        assert str(path) not in remaining
        bot.send_document.assert_awaited_once()

    async def test_parse_markdown_external_link_does_not_send_or_rewrite(self):
        bot = MagicMock()
        bot.send_document = AsyncMock()
        text = "See [docs](https://example.com/docs)."

        remaining = await bot_codex._parse_and_send_markers(123, text, None, bot)

        assert remaining == text
        bot.send_document.assert_not_awaited()

    async def test_parse_markdown_missing_relative_link_does_not_send_or_rewrite(self, tmp_path):
        workspace = tmp_path / "owner" / "repo"
        workspace.mkdir(parents=True)
        bot = MagicMock()
        bot.send_document = AsyncMock()
        text = "See [notes](notes.md)."

        with patch.object(bot_codex.codex_mgr, "workspace_path", return_value=workspace):
            remaining = await bot_codex._parse_and_send_markers(123, text, "owner/repo", bot)

        assert remaining == text
        bot.send_document.assert_not_awaited()


class TestListFiles:
    async def test_list_files_no_repo(self):
        update = _make_update(chat_id=201)
        ctx = _make_context()

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch("bot_codex.get_active_repo", return_value=None),
        ):
            await bot_codex.list_files(update, ctx)

        text = update.message.reply_text.call_args.args[0]
        assert "No repo set" in text

    async def test_list_files_workspace_missing(self, tmp_path):
        update = _make_update(chat_id=202)
        ctx = _make_context()
        missing = tmp_path / "missing"

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch("bot_codex.get_active_repo", return_value="owner/repo"),
            patch.object(bot_codex.codex_mgr, "workspace_path", return_value=missing),
        ):
            await bot_codex.list_files(update, ctx)

        text = update.message.reply_text.call_args.args[0]
        assert "not cloned" in text.lower()

    async def test_list_files_with_files(self, tmp_path):
        chat_id = 203
        workspace = tmp_path / "owner" / "repo"
        workspace.mkdir(parents=True)
        (workspace / "a.py").write_text("x = 1\n")
        (workspace / "node_modules").mkdir()
        (workspace / "node_modules" / "skip.js").write_text("skip")

        update = _make_update(chat_id=chat_id)
        ctx = _make_context()
        try:
            with (
                patch("bot_codex.is_authorized", return_value=True),
                patch("bot_codex.get_active_repo", return_value="owner/repo"),
                patch.object(bot_codex.codex_mgr, "workspace_path", return_value=workspace),
            ):
                await bot_codex.list_files(update, ctx)

            assert bot_codex._files_cache[chat_id] == [workspace / "a.py"]
            kwargs = update.message.reply_text.call_args.kwargs
            assert kwargs["reply_markup"] is not None
            assert "Recent files" in update.message.reply_text.call_args.args[0]
        finally:
            bot_codex._files_cache.pop(chat_id, None)


class TestInlineCallback:
    async def test_callback_dl_expired(self):
        chat_id = 301
        bot_codex._files_cache.pop(chat_id, None)
        update = _make_callback_update("dl:1", chat_id=chat_id)
        ctx = _make_context()

        with patch("bot_codex.is_authorized", return_value=True):
            await bot_codex.inline_callback(update, ctx)

        update.callback_query.edit_message_text.assert_awaited_once()
        assert "expired" in update.callback_query.edit_message_text.call_args.args[0].lower()

    async def test_callback_dl_sends_cached_file(self, tmp_path):
        chat_id = 302
        path = tmp_path / "result.txt"
        path.write_text("hello")
        bot_codex._files_cache[chat_id] = [path]
        update = _make_callback_update("dl:0", chat_id=chat_id)
        ctx = _make_context()
        try:
            with (
                patch("bot_codex.is_authorized", return_value=True),
                patch("bot_codex._send_file_to_user", new_callable=AsyncMock) as send_file,
            ):
                await bot_codex.inline_callback(update, ctx)

            send_file.assert_awaited_once_with(chat_id, path, ctx.bot)
        finally:
            bot_codex._files_cache.pop(chat_id, None)


class TestProgressExplanations:
    async def test_agent_message_sends_before_following_command(self):
        bot = object()
        on_event, state = bot_codex._make_event_handler(123, bot)

        with (
            patch("bot_codex._update_progress", new_callable=AsyncMock) as update_progress,
            patch("bot_codex.send_long_message", new_callable=AsyncMock) as send_long_message,
        ):
            await on_event(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I will inspect the working directory first."},
                }
            )
            update_progress.assert_not_awaited()
            send_long_message.assert_not_awaited()

            await on_event(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": "pwd", "status": "in_progress"},
                }
            )

        assert state["final_text"] is None
        send_long_message.assert_awaited_once_with(
            123, "I will inspect the working directory first.", bot, disable_notification=True
        )
        update_progress.assert_has_awaits([call(123, "$ pwd", bot)])

    async def test_final_agent_message_is_not_progress_without_more_work(self):
        on_event, state = bot_codex._make_event_handler(123, object())

        with (
            patch("bot_codex._update_progress", new_callable=AsyncMock) as update_progress,
            patch("bot_codex.send_long_message", new_callable=AsyncMock) as send_long_message,
        ):
            await on_event({"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}})
            await on_event({"type": "turn.completed", "usage": {}})

        assert state["final_text"] == "Done."
        update_progress.assert_not_awaited()
        send_long_message.assert_not_awaited()

    async def test_event_handler_marks_turn_activity(self):
        activity = asyncio.Event()
        on_event, _state = bot_codex._make_event_handler(123, object(), activity)

        await on_event({"type": "turn.started"})

        assert activity.is_set()


class TestIdleHeartbeat:
    async def test_posts_after_idle_interval_and_resets_after_activity(self):
        activity = asyncio.Event()
        bot = object()

        with (
            patch("bot_codex.CODEX_IDLE_HEARTBEAT_SECONDS", 0.01),
            patch("bot_codex.send_long_message", new_callable=AsyncMock) as send_long_message,
        ):
            task = asyncio.create_task(bot_codex._idle_heartbeat(123, bot, activity))
            try:
                await asyncio.sleep(0.015)
                assert send_long_message.await_count == 1

                activity.set()
                await asyncio.sleep(0)
                await asyncio.sleep(0.015)
                assert send_long_message.await_count == 2
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        send_long_message.assert_awaited_with(
            123,
            "Still working. No new Codex events for 1 minute(s). Use /cancel to interrupt.",
            bot,
            disable_notification=True,
        )


class TestLongRunningTurn:
    async def test_second_message_reports_busy_instead_of_waiting(self):
        chat_id = 401
        update = _make_update(chat_id=chat_id)
        update.message.text = "another request"
        update.message.caption = None
        update.message.photo = []
        update.message.document = None
        context = _make_context()
        lock = bot_codex._chat_lock(chat_id)

        await lock.acquire()
        try:
            with (
                patch("bot_codex.is_authorized", return_value=True),
                patch("bot_codex._dispatch_prompt", new_callable=AsyncMock) as dispatch,
            ):
                await bot_codex.handle_message(update, context)
        finally:
            lock.release()
            bot_codex._chat_locks.pop(chat_id, None)

        dispatch.assert_not_awaited()
        update.message.reply_text.assert_awaited_once_with(
            "Codex is still working on the previous request. Use /cancel to interrupt it."
        )


class TestCancellationCommands:
    async def test_cancel_uses_parent_only_interrupt(self):
        update = _make_update(chat_id=501)
        context = _make_context()

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch.object(bot_codex.codex_mgr, "interrupt", new_callable=AsyncMock, return_value=True) as interrupt,
            patch("bot_codex._clear_progress", new_callable=AsyncMock),
        ):
            await bot_codex.cancel_command(update, context)

        interrupt.assert_awaited_once_with(501, mark_pending=False)
        update.message.reply_text.assert_awaited_once_with("Cancelled current turn.")

    async def test_stop_keeps_hard_process_group_abort(self):
        update = _make_update(chat_id=502)
        context = _make_context()

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch.object(bot_codex.codex_mgr, "abort", new_callable=AsyncMock, return_value=True) as abort,
            patch("bot_codex._clear_progress", new_callable=AsyncMock),
        ):
            await bot_codex.stop_command(update, context)

        abort.assert_awaited_once_with(502, mark_pending=False)
        update.message.reply_text.assert_awaited_once_with("Stopped.")
