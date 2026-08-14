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
                patch("bot_codex.get_active_branch", return_value=None),
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


def _reset_queue_state(chat_id: int) -> None:
    bot_codex._chat_locks.pop(chat_id, None)
    bot_codex._pending_prompts.pop(chat_id, None)
    bot_codex._prompt_active.discard(chat_id)
    bot_codex._pending_steers.pop(chat_id, None)
    bot_codex._one_shot_mode.discard(chat_id)


class TestQueuedMessages:
    async def test_stream_message_steers_active_turn_instead_of_queueing(self):
        chat_id = 400
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        lock = bot_codex._chat_lock(chat_id)
        bot_codex._one_shot_mode.discard(chat_id)

        await lock.acquire()
        try:
            with (
                patch.object(bot_codex.app_server_mgr, "steer", new_callable=AsyncMock, return_value=True) as steer,
                patch("bot_codex._dispatch_prompt", new_callable=AsyncMock) as dispatch,
                patch("bot_codex.audit_log"),
            ):
                await bot_codex._queue_prompt(chat_id, "second", update, context)
        finally:
            lock.release()
            bot_codex._one_shot_mode.discard(chat_id)

        steer.assert_awaited_once_with(chat_id, "second")
        dispatch.assert_not_awaited()
        assert chat_id not in bot_codex._pending_prompts
        update.message.reply_text.assert_awaited_once_with("Added to the active Codex turn.")
        _reset_queue_state(chat_id)

    async def test_stream_message_waits_for_turn_started_then_steers(self):
        chat_id = 407
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        lock = bot_codex._chat_lock(chat_id)
        bot_codex._one_shot_mode.discard(chat_id)
        bot_codex._prompt_active.add(chat_id)
        await lock.acquire()

        try:
            with (
                patch.object(bot_codex.app_server_mgr, "steer", new_callable=AsyncMock) as steer,
                patch("bot_codex.audit_log"),
            ):
                steer.side_effect = [False, True]
                await bot_codex._queue_prompt(chat_id, "during setup", update, context)
        finally:
            lock.release()
            bot_codex._one_shot_mode.discard(chat_id)

        assert steer.await_count == 2
        assert chat_id not in bot_codex._pending_steers
        assert [call.args[0] for call in update.message.reply_text.await_args_list] == [
            "Waiting for the active Codex turn to accept this message…",
            "Added to the active Codex turn.",
        ]
        _reset_queue_state(chat_id)

    async def test_cancel_drops_message_waiting_to_steer(self):
        chat_id = 408
        token = object()
        bot_codex._pending_steers[chat_id] = {token}

        assert bot_codex._drop_queued(chat_id) == 1
        assert chat_id not in bot_codex._pending_steers
        _reset_queue_state(chat_id)

    async def test_stream_message_waits_out_control_lock_without_becoming_orphaned(self):
        chat_id = 406
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        lock = bot_codex._chat_lock(chat_id)
        bot_codex._one_shot_mode.discard(chat_id)
        await lock.acquire()

        try:
            with (
                patch.object(bot_codex.app_server_mgr, "steer", new_callable=AsyncMock, return_value=False),
                patch("bot_codex._dispatch_prompt", new_callable=AsyncMock, return_value=True) as dispatch,
            ):
                task = asyncio.create_task(bot_codex._queue_prompt(chat_id, "after status", update, context))
                await asyncio.sleep(0)
                lock.release()
                await task
        finally:
            if lock.locked():
                lock.release()
            bot_codex._one_shot_mode.discard(chat_id)

        dispatch.assert_awaited_once_with(chat_id, "after status", update, context)
        assert chat_id not in bot_codex._pending_prompts
        update.message.reply_text.assert_awaited_once_with("Waiting for the current Codex command to finish…")
        _reset_queue_state(chat_id)

    async def test_message_arriving_mid_turn_is_queued_not_rejected(self):
        chat_id = 401
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        lock = bot_codex._chat_lock(chat_id)
        bot_codex._one_shot_mode.add(chat_id)

        await lock.acquire()
        try:
            with patch("bot_codex._dispatch_prompt", new_callable=AsyncMock) as dispatch:
                await bot_codex._queue_prompt(chat_id, "second", update, context)
        finally:
            lock.release()

        dispatch.assert_not_awaited()
        assert [text for text, _ in bot_codex._pending_prompts[chat_id]] == ["second"]
        update.message.reply_text.assert_awaited_once_with("Queued (#1) — goes to Codex when this turn finishes.")
        _reset_queue_state(chat_id)

    async def test_queued_messages_are_merged_into_one_turn(self):
        """`codex exec resume` reloads the thread per turn, so N follow-ups should
        not cost N turns."""
        chat_id = 404
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        bot_codex._pending_prompts[chat_id] = [("also lint it", _make_update(chat_id=chat_id))]
        last = _make_update(chat_id=chat_id)
        bot_codex._pending_prompts[chat_id].append(("and push", last))

        with patch("bot_codex._dispatch_prompt", new_callable=AsyncMock, return_value=True) as dispatch:
            await bot_codex._queue_prompt(chat_id, "run the tests", update, context)

        assert [c.args[1] for c in dispatch.await_args_list] == ["run the tests", "also lint it\n\nand push"]
        # The merged turn replies against the most recent of the queued messages.
        assert dispatch.await_args_list[1].args[2] is last
        assert chat_id not in bot_codex._pending_prompts
        _reset_queue_state(chat_id)

    async def test_queue_is_bounded(self):
        chat_id = 402
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        lock = bot_codex._chat_lock(chat_id)
        bot_codex._one_shot_mode.add(chat_id)
        bot_codex._pending_prompts[chat_id] = [
            (f"queued {i}", _make_update(chat_id=chat_id)) for i in range(bot_codex.MAX_QUEUED_PROMPTS)
        ]

        await lock.acquire()
        try:
            with patch("bot_codex._dispatch_prompt", new_callable=AsyncMock) as dispatch:
                await bot_codex._queue_prompt(chat_id, "one too many", update, context)
        finally:
            lock.release()

        dispatch.assert_not_awaited()
        assert len(bot_codex._pending_prompts[chat_id]) == bot_codex.MAX_QUEUED_PROMPTS
        assert "already waiting" in update.message.reply_text.await_args.args[0]
        _reset_queue_state(chat_id)

    async def test_aborted_turn_drops_the_queue_instead_of_running_it(self):
        """Regression: stopping a turn used to feed the queue to Codex immediately,
        which looked exactly like the cancel having failed."""
        chat_id = 403
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        context.bot.send_message = AsyncMock()
        bot_codex._pending_prompts[chat_id] = [("queued work", _make_update(chat_id=chat_id))]

        with patch("bot_codex._dispatch_prompt", new_callable=AsyncMock, return_value=False) as dispatch:
            await bot_codex._queue_prompt(chat_id, "first", update, context)

        dispatch.assert_awaited_once()  # the queued message never ran
        assert chat_id not in bot_codex._pending_prompts
        context.bot.send_message.assert_awaited_once_with(chat_id=chat_id, text="Dropped 1 queued message.")
        _reset_queue_state(chat_id)

    async def test_cancel_clears_the_queue(self):
        chat_id = 405
        cancel_update = _make_update(chat_id=chat_id)
        context = _make_context()
        bot_codex._pending_prompts[chat_id] = [("a", _make_update()), ("b", _make_update())]
        bot_codex._one_shot_mode.add(chat_id)

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch.object(bot_codex.codex_mgr, "interrupt", new_callable=AsyncMock, return_value="cancelled"),
            patch("bot_codex._clear_progress", new_callable=AsyncMock),
            patch("bot_codex._stop_typing"),
        ):
            await bot_codex.cancel_command(cancel_update, context)

        assert chat_id not in bot_codex._pending_prompts
        cancel_update.message.reply_text.return_value.edit_text.assert_awaited_once_with(
            "Cancelled current turn. Dropped 2 queued messages."
        )
        _reset_queue_state(chat_id)


class TestCancellationCommands:
    async def test_cancel_uses_graceful_interrupt(self):
        update = _make_update(chat_id=501)
        context = _make_context()
        bot_codex._one_shot_mode.add(501)

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch.object(
                bot_codex.codex_mgr, "interrupt", new_callable=AsyncMock, return_value="cancelled"
            ) as interrupt,
            patch("bot_codex._clear_progress", new_callable=AsyncMock),
        ):
            await bot_codex.cancel_command(update, context)

        interrupt.assert_awaited_once_with(501, mark_pending=False)
        update.message.reply_text.return_value.edit_text.assert_awaited_once_with("Cancelled current turn.")
        bot_codex._one_shot_mode.discard(501)

    async def test_cancel_acknowledges_before_signalling(self):
        """Regression: /cancel used to sit silent for the whole SIGINT grace period."""
        update = _make_update(chat_id=504)
        context = _make_context()
        bot_codex._one_shot_mode.add(504)
        order: list[str] = []

        update.message.reply_text.side_effect = lambda *a, **k: order.append("ack") or AsyncMock()

        async def slow_interrupt(*_a, **_k):
            order.append("interrupt")
            return "cancelled"

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch.object(bot_codex.codex_mgr, "interrupt", new=slow_interrupt),
            patch("bot_codex._clear_progress", new_callable=AsyncMock),
        ):
            await bot_codex.cancel_command(update, context)

        assert order == ["ack", "interrupt"]
        bot_codex._one_shot_mode.discard(504)

    async def test_cancel_reports_a_forced_kill_honestly(self):
        update = _make_update(chat_id=503)
        context = _make_context()
        bot_codex._one_shot_mode.add(503)

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch.object(bot_codex.codex_mgr, "interrupt", new_callable=AsyncMock, return_value="forced"),
            patch("bot_codex._clear_progress", new_callable=AsyncMock),
        ):
            await bot_codex.cancel_command(update, context)

        update.message.reply_text.return_value.edit_text.assert_awaited_once_with(
            "Codex ignored the interrupt — killed it."
        )
        bot_codex._one_shot_mode.discard(503)

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


class TestStreamModeCommands:
    def test_stream_is_default_for_new_chat(self):
        chat_id = 600
        bot_codex._one_shot_mode.discard(chat_id)
        bot_codex._stream_mode_loaded.discard(chat_id)

        with patch("bot_codex.load_codex_stream_mode", return_value=True):
            assert bot_codex._uses_stream(chat_id) is True

    def test_persisted_nostream_mode_is_restored(self):
        chat_id = 606
        bot_codex._one_shot_mode.discard(chat_id)
        bot_codex._stream_mode_loaded.discard(chat_id)

        with patch("bot_codex.load_codex_stream_mode", return_value=False) as load:
            assert bot_codex._uses_stream(chat_id) is False
            assert bot_codex._uses_stream(chat_id) is False

        load.assert_called_once_with(chat_id)
        bot_codex._one_shot_mode.discard(chat_id)
        bot_codex._stream_mode_loaded.discard(chat_id)

    async def test_stream_enables_app_server_mode(self):
        chat_id = 601
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        bot_codex._one_shot_mode.add(chat_id)

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch("bot_codex.save_codex_stream_mode") as save_mode,
        ):
            await bot_codex.stream_command(update, context)

        assert chat_id not in bot_codex._one_shot_mode
        save_mode.assert_called_once_with(chat_id, True)
        assert "enabled" in update.message.reply_text.await_args.args[0]
        bot_codex._one_shot_mode.discard(chat_id)

    async def test_nostream_stops_app_server_and_restores_exec(self):
        chat_id = 602
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        bot_codex._one_shot_mode.discard(chat_id)

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch("bot_codex.save_codex_stream_mode") as save_mode,
            patch.object(bot_codex.app_server_mgr, "stop", new_callable=AsyncMock) as stop,
        ):
            await bot_codex.nostream_command(update, context)

        assert chat_id in bot_codex._one_shot_mode
        save_mode.assert_called_once_with(chat_id, False)
        stop.assert_awaited_once_with(chat_id)
        assert "One-shot mode enabled" in update.message.reply_text.await_args.args[0]

    async def test_dispatch_uses_app_server_when_stream_enabled(self):
        chat_id = 603
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        bot_codex._one_shot_mode.discard(chat_id)
        try:
            with (
                patch("bot_codex.get_active_repo", return_value="owner/repo"),
                patch("bot_codex.get_active_branch", return_value=None),
                patch("bot_codex.load_codex_session_id", return_value=None),
                patch.object(bot_codex.codex_mgr, "ensure_clone", new_callable=AsyncMock),
                patch.object(bot_codex.codex_mgr, "pull_latest", new_callable=AsyncMock),
                patch.object(bot_codex.app_server_mgr, "run_turn", new_callable=AsyncMock) as run_turn,
                patch("bot_codex._clear_progress", new_callable=AsyncMock),
                patch("bot_codex._start_typing"),
                patch("bot_codex._stop_typing"),
                patch("bot_codex._idle_heartbeat", new_callable=AsyncMock),
            ):
                assert await bot_codex._dispatch_prompt(chat_id, "hello", update, context) is True

            run_turn.assert_awaited_once()
        finally:
            bot_codex._one_shot_mode.discard(chat_id)

    async def test_double_slash_passes_single_slash_to_prompt_queue(self):
        chat_id = 604
        update = _make_update(chat_id=chat_id)
        update.message.text = "//status"
        update.message.caption = None
        update.message.photo = []
        update.message.document = None
        context = _make_context()
        bot_codex._one_shot_mode.discard(chat_id)
        try:
            with (
                patch("bot_codex.is_authorized", return_value=True),
                patch("bot_codex._queue_prompt", new_callable=AsyncMock) as queue,
            ):
                await bot_codex.handle_message(update, context)

            queue.assert_awaited_once_with(chat_id, "/status", update, context)
        finally:
            bot_codex._one_shot_mode.discard(chat_id)

    async def test_double_slash_passthrough_also_works_in_one_shot_mode(self):
        chat_id = 606
        update = _make_update(chat_id=chat_id)
        update.message.text = "//anything flexible"
        update.message.caption = None
        update.message.photo = []
        update.message.document = None
        context = _make_context()
        bot_codex._one_shot_mode.add(chat_id)

        with (
            patch("bot_codex.is_authorized", return_value=True),
            patch("bot_codex._queue_prompt", new_callable=AsyncMock) as queue,
        ):
            await bot_codex.handle_message(update, context)

        queue.assert_awaited_once_with(chat_id, "/anything flexible", update, context)
        bot_codex._one_shot_mode.discard(chat_id)

    async def test_fixed_goal_is_an_ordinary_telegram_command(self):
        chat_id = 605
        update = _make_update(chat_id=chat_id)
        update.message.text = "/goal Ship it"
        context = _make_context()
        context.args = ["Ship", "it"]
        bot_codex._one_shot_mode.discard(chat_id)
        try:
            with (
                patch("bot_codex.is_authorized", return_value=True),
                patch("bot_codex._handle_stream_slash", new_callable=AsyncMock) as control,
            ):
                await bot_codex.stream_control_command(update, context)

            control.assert_awaited_once_with(chat_id, "/goal Ship it", update, context)
        finally:
            bot_codex._one_shot_mode.discard(chat_id)

    async def test_new_turn_clears_an_abort_flag_left_by_a_previous_stop(self):
        """Regression: after /cancel then /stop, the next message was silently dropped.

        /stop armed the manager's abort flag even though the turn it belonged to
        had already finished. run_turn consumed the stale flag on the *next*
        message and raised CodexTurnAborted before Codex ever saw the prompt, so
        the user got no reply at all.
        """
        chat_id = 505
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        bot_codex.codex_mgr._aborted_chats.add(chat_id)
        bot_codex._one_shot_mode.add(chat_id)

        try:
            with (
                patch("bot_codex.get_active_repo", return_value="owner/repo"),
                patch("bot_codex.get_active_branch", return_value=None),
                patch("bot_codex._clear_progress", new_callable=AsyncMock),
                patch("bot_codex.audit_log"),
                patch.object(bot_codex.codex_mgr, "ensure_clone", new_callable=AsyncMock),
                patch.object(bot_codex.codex_mgr, "pull_latest", new_callable=AsyncMock),
                patch.object(bot_codex.codex_mgr, "run_turn", new_callable=AsyncMock) as run_turn,
                patch("bot_codex._start_typing"),
                patch("bot_codex._stop_typing"),
                patch("bot_codex.load_codex_session_id", return_value=None),
            ):
                await bot_codex._dispatch_prompt(chat_id, "do the thing", update, context)

            assert chat_id not in bot_codex.codex_mgr._aborted_chats
            run_turn.assert_awaited_once()
        finally:
            bot_codex.codex_mgr._aborted_chats.discard(chat_id)
            bot_codex._one_shot_mode.discard(chat_id)
