"""Tests for the Pulse autonomous agent feature."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────


def _make_update(chat_id=1001, user_id=42, text="/pulse"):
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


class TestPulsePersistence:
    def test_save_and_load_config(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_pulse_config, save_pulse_config

            save_pulse_config(1001, enabled=True, interval_minutes=30, quiet_start="22:00", quiet_end="07:00")
            config = load_pulse_config(1001)
            assert config is not None
            assert config["enabled"] is True
            assert config["interval_minutes"] == 30
            assert config["quiet_start"] == "22:00"
            assert config["quiet_end"] == "07:00"
            assert config["last_pulse_at"] is None
            assert config["last_pulse_summary"] is None

    def test_load_config_empty(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_pulse_config

            assert load_pulse_config(9999) is None

    def test_update_config(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_pulse_config, save_pulse_config

            save_pulse_config(1001, enabled=False, interval_minutes=60, quiet_start=None, quiet_end=None)
            save_pulse_config(1001, enabled=True, interval_minutes=45, quiet_start="23:00", quiet_end="08:00")
            config = load_pulse_config(1001)
            assert config["enabled"] is True
            assert config["interval_minutes"] == 45
            assert config["quiet_start"] == "23:00"

    def test_update_last_run(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_pulse_config, save_pulse_config, update_pulse_last_run

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            update_pulse_last_run(1001, "Checked calendar, nothing urgent")
            config = load_pulse_config(1001)
            assert config["last_pulse_at"] is not None
            assert config["last_pulse_summary"] == "Checked calendar, nothing urgent"

    def test_save_and_load_goals(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_pulse_goals, save_pulse_goal

            gid = save_pulse_goal(1001, "Watch for PR reviews", "high")
            assert gid is not None
            goals = load_pulse_goals(1001)
            assert len(goals) == 1
            assert goals[0]["id"] == gid
            assert goals[0]["goal"] == "Watch for PR reviews"
            assert goals[0]["priority"] == "high"
            assert goals[0]["enabled"] is True

    def test_load_goals_empty(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_pulse_goals

            assert load_pulse_goals(9999) == []

    def test_delete_goal(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import delete_pulse_goal, load_pulse_goals, save_pulse_goal

            gid = save_pulse_goal(1001, "Watch trains", "normal")
            assert delete_pulse_goal(gid, 1001) is True
            assert load_pulse_goals(1001) == []

    def test_delete_goal_wrong_chat(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import delete_pulse_goal, save_pulse_goal

            gid = save_pulse_goal(1001, "Watch trains", "normal")
            assert delete_pulse_goal(gid, 9999) is False

    def test_load_all_configs(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_all_pulse_configs, save_pulse_config

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            save_pulse_config(2002, enabled=True, interval_minutes=30, quiet_start=None, quiet_end=None)
            save_pulse_config(3003, enabled=False, interval_minutes=60, quiet_start=None, quiet_end=None)
            configs = load_all_pulse_configs()
            assert len(configs) == 2
            chat_ids = {c["chat_id"] for c in configs}
            assert chat_ids == {1001, 2002}

    def test_multiple_goals(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_pulse_goals, save_pulse_goal

            save_pulse_goal(1001, "Watch PRs", "high")
            save_pulse_goal(1001, "Check overdue tasks", "normal")
            save_pulse_goal(1001, "Monitor CI", "low")
            goals = load_pulse_goals(1001)
            assert len(goals) == 3


# ── manage_pulse tool dispatch ──────────────────────────────────────


class TestManagePulseTool:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_db):
        self.tmp_db = tmp_db
        self.patches = [
            patch("persistence.DB_PATH", tmp_db),
            patch("bot.ALLOWED_USER_IDS", {42}),
        ]
        for p in self.patches:
            p.start()
        # Clear module-level caches
        import bot

        bot._pulse_configs.clear()
        bot._pending_pulse_registrations.clear()
        bot._pending_pulse_unregistrations.clear()
        yield
        for p in self.patches:
            p.stop()

    def _make_block(self, input_dict):
        block = MagicMock()
        block.name = "manage_pulse"
        block.input = input_dict
        return block

    def test_add_goal(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "add_goal", "goal": "Watch my PRs", "priority": "high"}, 1001)
        assert "added" in result.lower()
        assert "Watch my PRs" in result

    def test_add_goal_no_text(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "add_goal", "goal": ""}, 1001)
        assert "error" in result.lower()

    def test_remove_goal(self):
        from persistence import save_pulse_goal

        gid = save_pulse_goal(1001, "Test goal", "normal")
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "remove_goal", "goal_id": gid}, 1001)
        assert "removed" in result.lower()

    def test_remove_goal_not_found(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "remove_goal", "goal_id": 999}, 1001)
        assert "not found" in result.lower()

    def test_list_goals(self):
        from persistence import save_pulse_goal

        save_pulse_goal(1001, "Watch PRs", "high")
        save_pulse_goal(1001, "Check tasks", "normal")
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "list_goals"}, 1001)
        assert "Watch PRs" in result
        assert "Check tasks" in result

    def test_list_goals_empty(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "list_goals"}, 1001)
        assert "no pulse goals" in result.lower()

    def test_enable(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "enable"}, 1001)
        assert "enabled" in result.lower()
        from persistence import load_pulse_config

        config = load_pulse_config(1001)
        assert config["enabled"] is True

    def test_disable(self):
        from persistence import save_pulse_config

        save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "disable"}, 1001)
        assert "disabled" in result.lower()

    def test_set_interval(self):
        from persistence import save_pulse_config

        save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "set_interval", "interval_minutes": 30}, 1001)
        assert "30" in result
        from persistence import load_pulse_config

        config = load_pulse_config(1001)
        assert config["interval_minutes"] == 30

    def test_set_interval_clamped(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "set_interval", "interval_minutes": 5}, 1001)
        assert "15" in result  # clamped to minimum 15

    def test_set_quiet_hours(self):
        from persistence import save_pulse_config

        save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "set_quiet_hours", "quiet_start": "22:00", "quiet_end": "07:00"}, 1001)
        assert "22:00" in result
        from persistence import load_pulse_config

        config = load_pulse_config(1001)
        assert config["quiet_start"] == "22:00"
        assert config["quiet_end"] == "07:00"

    def test_status(self):
        from persistence import save_pulse_config, save_pulse_goal

        save_pulse_config(1001, enabled=True, interval_minutes=45, quiet_start="22:00", quiet_end="07:00")
        save_pulse_goal(1001, "Watch PRs", "high")
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "status"}, 1001)
        assert "yes" in result.lower()
        assert "45" in result
        assert "Watch PRs" in result

    def test_status_not_configured(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "status"}, 1001)
        assert "not configured" in result.lower()

    def test_unknown_action(self):
        from bot import _handle_manage_pulse

        result = _handle_manage_pulse({"action": "explode"}, 1001)
        assert "unknown" in result.lower()


# ── Quiet hours logic ────────────────────────────────────────────────


class TestQuietHours:
    def test_overnight_quiet_in_range(self):
        from bot import _is_quiet_hours

        # 23:30 is within 22:00-07:00
        tz = datetime.UTC
        with patch("bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = datetime.datetime(2026, 2, 18, 23, 30, tzinfo=tz)
            mock_dt.UTC = datetime.UTC
            result = _is_quiet_hours("22:00", "07:00", tz)
        assert result is True

    def test_overnight_quiet_out_of_range(self):
        from bot import _is_quiet_hours

        # 12:00 is outside 22:00-07:00
        tz = datetime.UTC
        with patch("bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = datetime.datetime(2026, 2, 18, 12, 0, tzinfo=tz)
            mock_dt.UTC = datetime.UTC
            result = _is_quiet_hours("22:00", "07:00", tz)
        assert result is False

    def test_same_day_quiet_in_range(self):
        from bot import _is_quiet_hours

        # 10:00 is within 09:00-17:00
        tz = datetime.UTC
        with patch("bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = datetime.datetime(2026, 2, 18, 10, 0, tzinfo=tz)
            mock_dt.UTC = datetime.UTC
            result = _is_quiet_hours("09:00", "17:00", tz)
        assert result is True

    def test_no_quiet_hours(self):
        from bot import _is_quiet_hours

        tz = datetime.UTC
        assert _is_quiet_hours(None, None, tz) is False

    def test_invalid_quiet_hours(self):
        from bot import _is_quiet_hours

        tz = datetime.UTC
        assert _is_quiet_hours("invalid", "also_invalid", tz) is False


# ── Triage response parsing ──────────────────────────────────────────


class TestTriageParsing:
    @pytest.mark.asyncio
    async def test_triage_act(self, tmp_db):
        """Triage returns act=True when Haiku says to act."""
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '{"act": true, "reason": "Overdue task found"}'
        mock_response.content = [mock_block]

        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response),
        ):
            from persistence import save_pulse_config

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            from bot import _run_pulse_triage

            result = await _run_pulse_triage(1001)
            assert result["act"] is True
            assert "Overdue" in result["reason"]

    @pytest.mark.asyncio
    async def test_triage_no_act(self, tmp_db):
        """Triage returns act=False when nothing to do."""
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '{"act": false, "reason": ""}'
        mock_response.content = [mock_block]

        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response),
        ):
            from bot import _run_pulse_triage

            result = await _run_pulse_triage(1001)
            assert result["act"] is False

    @pytest.mark.asyncio
    async def test_triage_malformed_json(self, tmp_db):
        """Triage gracefully handles malformed JSON."""
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "I'm not sure, maybe check later?"
        mock_response.content = [mock_block]

        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response),
        ):
            from bot import _run_pulse_triage

            result = await _run_pulse_triage(1001)
            assert result["act"] is False

    @pytest.mark.asyncio
    async def test_triage_code_block_json(self, tmp_db):
        """Triage handles JSON wrapped in markdown code block."""
        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = '```json\n{"act": true, "reason": "Meeting in 30 min"}\n```'
        mock_response.content = [mock_block]

        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot._call_anthropic", new_callable=AsyncMock, return_value=mock_response),
        ):
            from persistence import save_pulse_config

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            from bot import _run_pulse_triage

            result = await _run_pulse_triage(1001)
            assert result["act"] is True


# ── /pulse command parsing ───────────────────────────────────────────


class TestPulseCommand:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_db):
        self.tmp_db = tmp_db
        self.patches = [
            patch("persistence.DB_PATH", tmp_db),
            patch("bot.ALLOWED_USER_IDS", {42}),
        ]
        for p in self.patches:
            p.start()
        import bot

        bot._pulse_configs.clear()
        yield
        for p in self.patches:
            p.stop()

    @pytest.mark.asyncio
    async def test_pulse_no_args_not_configured(self):
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=[])
        await pulse_command(update, ctx)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "not configured" in text.lower()

    @pytest.mark.asyncio
    async def test_pulse_on(self):
        from bot import pulse_command

        update = _make_update()
        jq = MagicMock()
        jq.run_repeating = MagicMock(return_value=MagicMock())
        ctx = _make_context(args=["on"], job_queue=jq)
        await pulse_command(update, ctx)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "enabled" in text.lower()

    @pytest.mark.asyncio
    async def test_pulse_off(self):
        from persistence import save_pulse_config

        save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=["off"])
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "disabled" in text.lower()

    @pytest.mark.asyncio
    async def test_pulse_every(self):
        from persistence import save_pulse_config

        save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        from bot import pulse_command

        update = _make_update()
        jq = MagicMock()
        jq.run_repeating = MagicMock(return_value=MagicMock())
        ctx = _make_context(args=["every", "30m"], job_queue=jq)
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "30" in text

    @pytest.mark.asyncio
    async def test_pulse_every_hours(self):
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=["every", "2h"])
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "120" in text

    @pytest.mark.asyncio
    async def test_pulse_quiet(self):
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=["quiet", "22:00-07:00"])
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "22:00" in text

    @pytest.mark.asyncio
    async def test_pulse_goals(self):
        from persistence import save_pulse_goal

        save_pulse_goal(1001, "Watch PRs", "high")
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=["goals"])
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Watch PRs" in text

    @pytest.mark.asyncio
    async def test_pulse_remove_goal(self):
        from persistence import save_pulse_goal

        gid = save_pulse_goal(1001, "Watch PRs", "high")
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=["remove", str(gid)])
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "removed" in text.lower()

    @pytest.mark.asyncio
    async def test_pulse_status_with_config(self):
        from persistence import save_pulse_config, save_pulse_goal

        save_pulse_config(1001, enabled=True, interval_minutes=45, quiet_start="22:00", quiet_end="07:00")
        save_pulse_goal(1001, "Watch CI", "normal")
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=[])
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ON" in text
        assert "45" in text
        assert "Watch CI" in text

    @pytest.mark.asyncio
    async def test_pulse_unknown_subcmd(self):
        from bot import pulse_command

        update = _make_update()
        ctx = _make_context(args=["explode"])
        await pulse_command(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "usage" in text.lower()


# ── Triage context building ──────────────────────────────────────────


class TestTriageContext:
    @pytest.mark.asyncio
    async def test_builds_context_with_goals(self, tmp_db):
        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot.calendar_client", None),
            patch("bot.tasks_client", None),
        ):
            from persistence import save_pulse_config, save_pulse_goal

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            save_pulse_goal(1001, "Watch PRs", "high")
            from bot import _build_triage_context

            context = await _build_triage_context(1001)
            assert "Watch PRs" in context
            assert "Time:" in context

    @pytest.mark.asyncio
    async def test_builds_context_with_last_summary(self, tmp_db):
        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot.calendar_client", None),
            patch("bot.tasks_client", None),
        ):
            from persistence import save_pulse_config, update_pulse_last_run

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            update_pulse_last_run(1001, "All clear, no issues found")
            from bot import _build_triage_context

            context = await _build_triage_context(1001)
            assert "All clear" in context


# ── Pulse job execution ─────────────────────────────────────────────


class TestPulseJob:
    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import save_pulse_config

            save_pulse_config(1001, enabled=False, interval_minutes=60, quiet_start=None, quiet_end=None)
            from bot import _run_pulse

            ctx = MagicMock()
            ctx.job.data = {"chat_id": 1001}
            ctx.bot = AsyncMock()

            with patch("bot._run_pulse_triage", new_callable=AsyncMock) as mock_triage:
                await _run_pulse(ctx)
                mock_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_quiet_hours(self, tmp_db):
        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot._is_quiet_hours", return_value=True),
        ):
            from persistence import save_pulse_config, save_pulse_goal

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start="22:00", quiet_end="07:00")
            save_pulse_goal(1001, "Watch PRs", "high")
            from bot import _run_pulse

            ctx = MagicMock()
            ctx.job.data = {"chat_id": 1001}
            ctx.bot = AsyncMock()

            with patch("bot._run_pulse_triage", new_callable=AsyncMock) as mock_triage:
                await _run_pulse(ctx)
                mock_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_no_goals(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import save_pulse_config

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            from bot import _run_pulse

            ctx = MagicMock()
            ctx.job.data = {"chat_id": 1001}
            ctx.bot = AsyncMock()

            with patch("bot._run_pulse_triage", new_callable=AsyncMock) as mock_triage:
                await _run_pulse(ctx)
                mock_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_triage_no_act_skips_action(self, tmp_db):
        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot._is_quiet_hours", return_value=False),
        ):
            from persistence import save_pulse_config, save_pulse_goal

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            save_pulse_goal(1001, "Watch PRs", "high")
            from bot import _run_pulse

            ctx = MagicMock()
            ctx.job.data = {"chat_id": 1001}
            ctx.bot = AsyncMock()

            with (
                patch("bot._run_pulse_triage", new_callable=AsyncMock, return_value={"act": False, "reason": ""}),
                patch("bot._run_pulse_action", new_callable=AsyncMock) as mock_action,
            ):
                await _run_pulse(ctx)
                mock_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_triage_act_triggers_action(self, tmp_db):
        with (
            patch("persistence.DB_PATH", tmp_db),
            patch("bot._is_quiet_hours", return_value=False),
        ):
            from persistence import save_pulse_config, save_pulse_goal

            save_pulse_config(1001, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
            save_pulse_goal(1001, "Watch PRs", "high")
            from bot import _run_pulse

            ctx = MagicMock()
            ctx.job.data = {"chat_id": 1001}
            ctx.bot = AsyncMock()

            with (
                patch(
                    "bot._run_pulse_triage",
                    new_callable=AsyncMock,
                    return_value={"act": True, "reason": "Overdue task"},
                ),
                patch("bot._run_pulse_action", new_callable=AsyncMock) as mock_action,
            ):
                await _run_pulse(ctx)
                mock_action.assert_called_once_with(ctx.bot, 1001, {"act": True, "reason": "Overdue task"})
