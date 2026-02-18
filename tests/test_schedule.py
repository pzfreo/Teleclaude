"""Tests for user-configurable scheduled jobs."""

from unittest.mock import AsyncMock, MagicMock, patch

# ── Helpers ──────────────────────────────────────────────────────────


def _make_update(chat_id=1001, user_id=42, text="/schedule"):
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


class TestSchedulePersistence:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_schedules, save_schedule

            sid = save_schedule(1001, "daily", "08:00", "Morning briefing")
            assert sid is not None
            schedules = load_schedules(1001)
            assert len(schedules) == 1
            assert schedules[0]["id"] == sid
            assert schedules[0]["interval_type"] == "daily"
            assert schedules[0]["interval_value"] == "08:00"
            assert schedules[0]["prompt"] == "Morning briefing"
            assert schedules[0]["enabled"] == 1

    def test_load_empty(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_schedules

            assert load_schedules(9999) == []

    def test_load_all_schedules(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_all_schedules, save_schedule

            save_schedule(1001, "daily", "08:00", "Briefing")
            save_schedule(2002, "every", "4h", "Check tasks")
            all_s = load_all_schedules()
            assert len(all_s) == 2
            chat_ids = {s["chat_id"] for s in all_s}
            assert chat_ids == {1001, 2002}

    def test_delete_schedule(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import delete_schedule, load_schedules, save_schedule

            sid = save_schedule(1001, "daily", "08:00", "Briefing")
            assert delete_schedule(sid, 1001) is True
            assert load_schedules(1001) == []

    def test_delete_wrong_chat(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import delete_schedule, load_schedules, save_schedule

            sid = save_schedule(1001, "daily", "08:00", "Briefing")
            assert delete_schedule(sid, 9999) is False
            assert len(load_schedules(1001)) == 1

    def test_delete_nonexistent(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import delete_schedule

            assert delete_schedule(999, 1001) is False

    def test_multiple_schedules_per_chat(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_schedules, save_schedule

            save_schedule(1001, "daily", "08:00", "Morning")
            save_schedule(1001, "daily", "18:00", "Evening")
            save_schedule(1001, "every", "2h", "Check tasks")
            schedules = load_schedules(1001)
            assert len(schedules) == 3

    def test_schedules_table_in_init_db(self, tmp_db):
        import sqlite3

        conn = sqlite3.connect(str(tmp_db))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "schedules" in tables


# ── Schedule command handler tests ──────────────────────────────────


class TestScheduleCommand:
    async def test_no_args_shows_usage(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=[])
        with patch("bot.is_authorized", return_value=True):
            await schedule_command(update, ctx)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    async def test_list_empty(self, tmp_db):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["list"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.load_schedules", return_value=[]),
        ):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No schedules" in text

    async def test_list_with_schedules(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["list"])
        mock_schedules = [
            {"id": 1, "interval_type": "daily", "interval_value": "08:00", "prompt": "Morning briefing"},
            {"id": 2, "interval_type": "every", "interval_value": "4h", "prompt": "Check tasks"},
        ]
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.load_schedules", return_value=mock_schedules),
        ):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "#1" in text
        assert "#2" in text
        assert "daily at 08:00" in text
        assert "every 4h" in text

    async def test_create_daily(self, tmp_db):
        from bot import schedule_command

        update = _make_update()
        job_queue = MagicMock()
        job_queue.run_daily.return_value = MagicMock()
        ctx = _make_context(args=["daily", "08:00", "Morning", "briefing"], job_queue=job_queue)
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.save_schedule", return_value=1) as mock_save,
            patch("bot._register_schedule") as mock_register,
        ):
            await schedule_command(update, ctx)
        mock_save.assert_called_once_with(1001, "daily", "08:00", "Morning briefing")
        mock_register.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "#1" in text
        assert "daily at 08:00" in text

    async def test_create_daily_invalid_time(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["daily", "25:99", "Bad", "time"])
        with patch("bot.is_authorized", return_value=True):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Invalid time" in text

    async def test_create_daily_missing_prompt(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["daily", "08:00"])
        with patch("bot.is_authorized", return_value=True):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    async def test_create_every(self, tmp_db):
        from bot import schedule_command

        update = _make_update()
        job_queue = MagicMock()
        job_queue.run_repeating.return_value = MagicMock()
        ctx = _make_context(args=["every", "4h", "Check", "tasks"], job_queue=job_queue)
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.save_schedule", return_value=2) as mock_save,
            patch("bot._register_schedule") as mock_register,
        ):
            await schedule_command(update, ctx)
        mock_save.assert_called_once_with(1001, "every", "4h", "Check tasks")
        mock_register.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "#2" in text
        assert "every 4h" in text

    async def test_create_every_invalid_interval(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["every", "4m", "Bad", "interval"])
        with patch("bot.is_authorized", return_value=True):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Invalid interval" in text

    async def test_create_every_out_of_range(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["every", "30h", "Too", "long"])
        with patch("bot.is_authorized", return_value=True):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "between 1h and 24h" in text

    async def test_remove(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["remove", "5"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.delete_schedule", return_value=True) as mock_del,
            patch("bot._unregister_schedule") as mock_unreg,
        ):
            await schedule_command(update, ctx)
        mock_del.assert_called_once_with(5, 1001)
        mock_unreg.assert_called_once_with(5)
        text = update.message.reply_text.call_args[0][0]
        assert "#5 removed" in text

    async def test_remove_with_hash(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["remove", "#5"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.delete_schedule", return_value=True),
            patch("bot._unregister_schedule"),
        ):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "#5 removed" in text

    async def test_remove_not_found(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["remove", "99"])
        with (
            patch("bot.is_authorized", return_value=True),
            patch("bot.delete_schedule", return_value=False),
        ):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text

    async def test_remove_missing_id(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["remove"])
        with patch("bot.is_authorized", return_value=True):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    async def test_unknown_subcommand(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["foobar"])
        with patch("bot.is_authorized", return_value=True):
            await schedule_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Unknown subcommand" in text

    async def test_unauthorized(self):
        from bot import schedule_command

        update = _make_update()
        ctx = _make_context(args=["list"])
        with patch("bot.is_authorized", return_value=False):
            await schedule_command(update, ctx)
        update.message.reply_text.assert_not_called()


# ── Register/unregister tests ───────────────────────────────────────


class TestRegisterSchedule:
    def test_register_daily(self):
        from bot import _register_schedule, _scheduled_jobs

        job_queue = MagicMock()
        mock_job = MagicMock()
        job_queue.run_daily.return_value = mock_job

        schedule = {
            "id": 1,
            "chat_id": 1001,
            "interval_type": "daily",
            "interval_value": "08:00",
            "prompt": "Morning briefing",
        }

        try:
            _register_schedule(job_queue, schedule)
            job_queue.run_daily.assert_called_once()
            assert _scheduled_jobs[1] is mock_job
        finally:
            _scheduled_jobs.pop(1, None)

    def test_register_every(self):
        from bot import _register_schedule, _scheduled_jobs

        job_queue = MagicMock()
        mock_job = MagicMock()
        job_queue.run_repeating.return_value = mock_job

        schedule = {
            "id": 2,
            "chat_id": 1001,
            "interval_type": "every",
            "interval_value": "4h",
            "prompt": "Check tasks",
        }

        try:
            _register_schedule(job_queue, schedule)
            job_queue.run_repeating.assert_called_once()
            call_kwargs = job_queue.run_repeating.call_args
            assert call_kwargs.kwargs["interval"] == 4 * 3600
            assert _scheduled_jobs[2] is mock_job
        finally:
            _scheduled_jobs.pop(2, None)

    def test_unregister(self):
        from bot import _scheduled_jobs, _unregister_schedule

        mock_job = MagicMock()
        _scheduled_jobs[99] = mock_job

        _unregister_schedule(99)
        mock_job.schedule_removal.assert_called_once()
        assert 99 not in _scheduled_jobs

    def test_unregister_nonexistent(self):
        from bot import _unregister_schedule

        # Should not raise
        _unregister_schedule(99999)


# ── run_scheduled_prompt tests ──────────────────────────────────────


class TestRunScheduledPrompt:
    async def test_simple_text_response(self):
        from bot import run_scheduled_prompt

        mock_bot = AsyncMock()
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Here is your briefing."
        mock_response.content = [text_block]

        with (
            patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response),
            patch("bot.get_active_repo", return_value=None),
        ):
            await run_scheduled_prompt(mock_bot, 1001, "Give me a briefing")

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs["text"]
        assert "briefing" in text.lower()

    async def test_generate_briefing_delegates(self):
        """generate_briefing should call run_scheduled_prompt."""
        from bot import generate_briefing

        mock_bot = AsyncMock()
        with patch("bot.run_scheduled_prompt", new_callable=AsyncMock) as mock_run:
            await generate_briefing(mock_bot, 1001)
        mock_run.assert_called_once()
        assert mock_run.call_args[0][1] == 1001


# ── Startup loading tests ───────────────────────────────────────────


class TestLoadSchedulesOnStartup:
    async def test_loads_from_db(self):
        from bot import _load_schedules_on_startup

        app = MagicMock()
        app.job_queue = MagicMock()
        app.job_queue.run_daily.return_value = MagicMock()

        mock_schedules = [
            {
                "id": 1,
                "chat_id": 1001,
                "interval_type": "daily",
                "interval_value": "08:00",
                "prompt": "Morning briefing",
            }
        ]

        with (
            patch("bot.DAILY_BRIEFING_TIME", ""),
            patch("bot.load_all_schedules", return_value=mock_schedules),
            patch("bot._register_schedule") as mock_reg,
            patch("bot.load_all_monitors", return_value=[]),
            patch("bot.load_all_pulse_configs", return_value=[]),
        ):
            await _load_schedules_on_startup(app)

        mock_reg.assert_called_once_with(app.job_queue, mock_schedules[0])

    async def test_auto_migrates_briefing_time(self):
        from bot import _load_schedules_on_startup

        app = MagicMock()
        app.job_queue = MagicMock()
        app.job_queue.run_daily.return_value = MagicMock()

        with (
            patch("bot.DAILY_BRIEFING_TIME", "08:00"),
            patch("bot.ALLOWED_USER_IDS", {42}),
            patch(
                "bot.load_all_schedules",
                side_effect=[
                    [],
                    [
                        {
                            "id": 1,
                            "chat_id": 42,
                            "interval_type": "daily",
                            "interval_value": "08:00",
                            "prompt": "briefing",
                        }
                    ],
                ],
            ),
            patch("bot.save_schedule", return_value=1) as mock_save,
            patch("bot._register_schedule"),
            patch("bot.load_all_monitors", return_value=[]),
            patch("bot.load_all_pulse_configs", return_value=[]),
        ):
            await _load_schedules_on_startup(app)

        mock_save.assert_called_once()
        assert mock_save.call_args[0][0] == 42
        assert mock_save.call_args[0][1] == "daily"
        assert mock_save.call_args[0][2] == "08:00"

    async def test_no_job_queue(self):
        from bot import _load_schedules_on_startup

        app = MagicMock()
        app.job_queue = None
        # Should not raise
        await _load_schedules_on_startup(app)

    async def test_skips_migration_if_schedules_exist(self):
        from bot import _load_schedules_on_startup

        app = MagicMock()
        app.job_queue = MagicMock()
        app.job_queue.run_daily.return_value = MagicMock()

        existing = [
            {
                "id": 1,
                "chat_id": 42,
                "interval_type": "daily",
                "interval_value": "09:00",
                "prompt": "Custom briefing",
            }
        ]

        with (
            patch("bot.DAILY_BRIEFING_TIME", "08:00"),
            patch("bot.ALLOWED_USER_IDS", {42}),
            patch("bot.load_all_schedules", return_value=existing),
            patch("bot.save_schedule") as mock_save,
            patch("bot._register_schedule"),
            patch("bot.load_all_monitors", return_value=[]),
            patch("bot.load_all_pulse_configs", return_value=[]),
        ):
            await _load_schedules_on_startup(app)

        mock_save.assert_not_called()
