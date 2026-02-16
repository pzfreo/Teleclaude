"""Tests for the schedule_check tool and monitor system."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

# ── Helpers ──────────────────────────────────────────────────────────


def _make_update(chat_id=1001, user_id=42, text="/monitors"):
    update = MagicMock()
    update.effective_chat = AsyncMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    update.message = msg
    return update


def _make_context(bot=None, args=None, job_queue=None):
    ctx = MagicMock()
    ctx.bot = bot or AsyncMock()
    ctx.args = args or []
    ctx.application.job_queue = job_queue
    return ctx


# ── Persistence CRUD tests ──────────────────────────────────────────


class TestMonitorPersistence:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_monitors, save_monitor

            mid = save_monitor(
                chat_id=1001,
                check_prompt="Check trains",
                notify_condition="Any delay > 5 min",
                interval_minutes=10,
                expires_at=time.time() + 3600,
                summary="CHI→VIC trains",
            )
            assert mid is not None
            monitors = load_monitors(1001)
            assert len(monitors) == 1
            assert monitors[0]["id"] == mid
            assert monitors[0]["check_prompt"] == "Check trains"
            assert monitors[0]["notify_condition"] == "Any delay > 5 min"
            assert monitors[0]["interval_minutes"] == 10
            assert monitors[0]["summary"] == "CHI→VIC trains"
            assert monitors[0]["last_result"] is None
            assert monitors[0]["enabled"] == 1

    def test_load_empty(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_monitors

            assert load_monitors(9999) == []

    def test_load_all_monitors(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_all_monitors, save_monitor

            save_monitor(1001, "Check A", "cond A", 10, time.time() + 3600, "Monitor A")
            save_monitor(2002, "Check B", "cond B", 15, time.time() + 7200, "Monitor B")
            all_m = load_all_monitors()
            assert len(all_m) == 2
            chat_ids = {m["chat_id"] for m in all_m}
            assert chat_ids == {1001, 2002}

    def test_delete_monitor(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import delete_monitor, load_monitors, save_monitor

            mid = save_monitor(1001, "Check", "cond", 10, time.time() + 3600, "Test")
            assert delete_monitor(mid, 1001) is True
            assert load_monitors(1001) == []

    def test_delete_wrong_chat(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import delete_monitor, load_monitors, save_monitor

            mid = save_monitor(1001, "Check", "cond", 10, time.time() + 3600, "Test")
            assert delete_monitor(mid, 9999) is False
            assert len(load_monitors(1001)) == 1

    def test_update_result(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_monitors, save_monitor, update_monitor_result

            mid = save_monitor(1001, "Check", "cond", 10, time.time() + 3600, "Test")
            update_monitor_result(mid, "Train data: all on time")
            monitors = load_monitors(1001)
            assert monitors[0]["last_result"] == "Train data: all on time"

    def test_disable_monitor(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import disable_monitor, load_monitors, save_monitor

            mid = save_monitor(1001, "Check", "cond", 10, time.time() + 3600, "Test")
            disable_monitor(mid)
            # Disabled monitors don't show in load_monitors (enabled=1 filter)
            assert load_monitors(1001) == []

    def test_count_monitors(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import count_monitors, save_monitor

            assert count_monitors(1001) == 0
            save_monitor(1001, "Check A", "cond", 10, time.time() + 3600, "A")
            save_monitor(1001, "Check B", "cond", 10, time.time() + 3600, "B")
            assert count_monitors(1001) == 2

    def test_monitors_table_in_init_db(self, tmp_db):
        import sqlite3

        conn = sqlite3.connect(str(tmp_db))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "monitors" in tables


# ── _handle_schedule_check tests ─────────────────────────────────────


class TestHandleScheduleCheck:
    def test_creates_monitor(self):
        from bot import _handle_schedule_check, _pending_monitor_registrations

        tool_input = {
            "check_prompt": "Check train departures from CHI",
            "notify_condition": "Any train delayed or cancelled",
            "interval_minutes": 10,
            "expires_at": "2099-01-01T12:00:00",
            "summary": "CHI train delays",
        }
        with (
            patch("bot.count_monitors", return_value=0),
            patch("bot.save_monitor", return_value=42),
        ):
            result = _handle_schedule_check(tool_input, 1001)

        assert "Monitor #42 created" in result
        assert "CHI train delays" in result
        # Should have queued a registration
        assert len(_pending_monitor_registrations) >= 1
        reg = _pending_monitor_registrations.pop()
        assert reg["id"] == 42
        assert reg["interval_minutes"] == 10

    def test_rejects_over_limit(self):
        from bot import _handle_schedule_check

        tool_input = {
            "check_prompt": "Check something",
            "notify_condition": "Something changes",
            "interval_minutes": 10,
            "expires_at": "2099-01-01T12:00:00",
            "summary": "Over limit",
        }
        with patch("bot.count_monitors", return_value=5):
            result = _handle_schedule_check(tool_input, 1001)

        assert "limit reached" in result.lower()

    def test_clamps_interval(self):
        from bot import _handle_schedule_check, _pending_monitor_registrations

        tool_input = {
            "check_prompt": "Check something",
            "notify_condition": "Change detected",
            "interval_minutes": 1,  # below minimum
            "expires_at": "2099-01-01T12:00:00",
            "summary": "Clamped interval",
        }
        with (
            patch("bot.count_monitors", return_value=0),
            patch("bot.save_monitor", return_value=1) as mock_save,
        ):
            _handle_schedule_check(tool_input, 1001)

        # interval_minutes should be clamped to 5
        assert mock_save.call_args.kwargs["interval_minutes"] == 5
        _pending_monitor_registrations.clear()

    def test_caps_expiry_at_24h(self):
        from bot import _handle_schedule_check, _pending_monitor_registrations

        tool_input = {
            "check_prompt": "Check something",
            "notify_condition": "Change",
            "interval_minutes": 10,
            "expires_at": "2099-12-31T23:59:59",  # far future
            "summary": "Capped expiry",
        }
        with (
            patch("bot.count_monitors", return_value=0),
            patch("bot.save_monitor", return_value=1) as mock_save,
        ):
            _handle_schedule_check(tool_input, 1001)

        # expires_at should be capped to ~24h from now
        expires_at = mock_save.call_args.kwargs["expires_at"]
        assert expires_at <= time.time() + 24 * 3600 + 60  # small margin
        _pending_monitor_registrations.clear()

    def test_missing_required_fields(self):
        from bot import _handle_schedule_check

        tool_input = {"interval_minutes": 10, "summary": "Incomplete"}
        with patch("bot.count_monitors", return_value=0):
            result = _handle_schedule_check(tool_input, 1001)
        assert "Error" in result


# ── Register/unregister monitor tests ────────────────────────────────


class TestRegisterMonitor:
    def test_register(self):
        from bot import _monitor_jobs, _register_monitor

        job_queue = MagicMock()
        mock_job = MagicMock()
        job_queue.run_repeating.return_value = mock_job

        monitor = {
            "id": 1,
            "chat_id": 1001,
            "check_prompt": "Check trains",
            "notify_condition": "Delay",
            "interval_minutes": 10,
            "expires_at": time.time() + 3600,
            "summary": "Train monitor",
            "last_result": None,
        }

        try:
            _register_monitor(job_queue, monitor)
            job_queue.run_repeating.assert_called_once()
            call_kwargs = job_queue.run_repeating.call_args
            assert call_kwargs.kwargs["interval"] == 600  # 10 * 60
            assert _monitor_jobs[1] is mock_job
        finally:
            _monitor_jobs.pop(1, None)

    def test_unregister(self):
        from bot import _monitor_jobs, _unregister_monitor

        mock_job = MagicMock()
        _monitor_jobs[99] = mock_job

        _unregister_monitor(99)
        mock_job.schedule_removal.assert_called_once()
        assert 99 not in _monitor_jobs

    def test_unregister_nonexistent(self):
        from bot import _unregister_monitor

        # Should not raise
        _unregister_monitor(99999)


# ── Monitor job execution tests ──────────────────────────────────────


class TestRunMonitorJob:
    async def test_first_run_captures_baseline(self):
        from bot import _run_monitor_job

        context = MagicMock()
        context.job.data = {
            "id": 1,
            "chat_id": 1001,
            "check_prompt": "Check trains",
            "notify_condition": "Delay",
            "summary": "Train monitor",
            "expires_at": time.time() + 3600,
            "last_result": None,
        }
        context.bot = AsyncMock()

        with (
            patch("bot._run_monitor_prompt", new_callable=AsyncMock, return_value="All trains on time"),
            patch("bot.update_monitor_result") as mock_update,
        ):
            await _run_monitor_job(context)

        # First run: baseline stored, no notification sent
        mock_update.assert_called_once_with(1, "All trains on time")
        context.bot.send_message.assert_not_called()
        assert context.job.data["last_result"] == "All trains on time"

    async def test_no_change_no_notification(self):
        from bot import _run_monitor_job

        context = MagicMock()
        context.job.data = {
            "id": 1,
            "chat_id": 1001,
            "check_prompt": "Check trains",
            "notify_condition": "Delay",
            "summary": "Train monitor",
            "expires_at": time.time() + 3600,
            "last_result": "All on time",
        }
        context.bot = AsyncMock()

        with (
            patch("bot._run_monitor_prompt", new_callable=AsyncMock, return_value="All on time still"),
            patch("bot._compare_monitor_results", new_callable=AsyncMock, return_value=(False, None)),
            patch("bot.update_monitor_result"),
        ):
            await _run_monitor_job(context)

        # No change — no message sent
        context.bot.send_message.assert_not_called()

    async def test_change_sends_notification(self):
        from bot import _run_monitor_job

        context = MagicMock()
        context.job.data = {
            "id": 1,
            "chat_id": 1001,
            "check_prompt": "Check trains",
            "notify_condition": "Delay",
            "summary": "Train monitor",
            "expires_at": time.time() + 3600,
            "last_result": "All on time",
        }
        context.bot = AsyncMock()

        with (
            patch("bot._run_monitor_prompt", new_callable=AsyncMock, return_value="10:15 delayed by 20 min"),
            patch(
                "bot._compare_monitor_results",
                new_callable=AsyncMock,
                return_value=(True, "The 10:15 is now delayed by 20 minutes"),
            ),
            patch("bot.update_monitor_result"),
            patch("bot.send_long_message", new_callable=AsyncMock) as mock_send,
        ):
            await _run_monitor_job(context)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        assert "Train monitor" in msg
        assert "10:15" in msg

    async def test_expired_monitor_disables_and_notifies(self):
        from bot import _run_monitor_job

        context = MagicMock()
        context.job.data = {
            "id": 1,
            "chat_id": 1001,
            "check_prompt": "Check trains",
            "notify_condition": "Delay",
            "summary": "Expired monitor",
            "expires_at": time.time() - 100,  # already expired
            "last_result": None,
        }
        context.bot = AsyncMock()

        with (
            patch("bot._unregister_monitor") as mock_unreg,
            patch("bot.disable_monitor") as mock_disable,
        ):
            await _run_monitor_job(context)

        mock_unreg.assert_called_once_with(1)
        mock_disable.assert_called_once_with(1)
        context.bot.send_message.assert_called_once()
        text = context.bot.send_message.call_args.kwargs["text"]
        assert "expired" in text.lower()


# ── Comparison logic tests ───────────────────────────────────────────


class TestCompareMonitorResults:
    async def test_no_change(self):
        from bot import _compare_monitor_results

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "NO_CHANGE"
        mock_response.content = [text_block]

        with patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response):
            should_notify, msg = await _compare_monitor_results("old", "new", "condition", "summary")

        assert should_notify is False
        assert msg is None

    async def test_change_detected(self):
        from bot import _compare_monitor_results

        mock_response = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "The 10:15 service is now delayed by 20 minutes"
        mock_response.content = [text_block]

        with patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response):
            should_notify, msg = await _compare_monitor_results(
                "All on time", "10:15 delayed 20 min", "Any delay", "Train monitor"
            )

        assert should_notify is True
        assert "10:15" in msg


# ── /monitors command tests ──────────────────────────────────────────


class TestMonitorsCommand:
    async def test_list_empty(self):
        from bot import monitors_command

        update = _make_update()
        ctx = _make_context(args=["list"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.load_monitors", return_value=[]),
        ):
            await monitors_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No active monitors" in text

    async def test_list_with_monitors(self):
        from bot import monitors_command

        update = _make_update()
        ctx = _make_context(args=[])  # default is list
        mock_monitors = [
            {
                "id": 1,
                "summary": "Train delays",
                "interval_minutes": 10,
                "expires_at": time.time() + 3600,
            },
        ]
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.load_monitors", return_value=mock_monitors),
        ):
            await monitors_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "#1" in text
        assert "Train delays" in text
        assert "10m" in text

    async def test_remove(self):
        from bot import monitors_command

        update = _make_update()
        ctx = _make_context(args=["remove", "5"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.delete_monitor", return_value=True) as mock_del,
            patch("bot._unregister_monitor") as mock_unreg,
        ):
            await monitors_command(update, ctx)
        mock_del.assert_called_once_with(5, 1001)
        mock_unreg.assert_called_once_with(5)
        text = update.message.reply_text.call_args[0][0]
        assert "#5 removed" in text

    async def test_remove_not_found(self):
        from bot import monitors_command

        update = _make_update()
        ctx = _make_context(args=["remove", "99"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.delete_monitor", return_value=False),
        ):
            await monitors_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text

    async def test_unauthorized(self):
        from bot import monitors_command

        update = _make_update()
        ctx = _make_context(args=["list"])
        with patch("bot.is_authorized", return_value=False):
            await monitors_command(update, ctx)
        update.message.reply_text.assert_not_called()
