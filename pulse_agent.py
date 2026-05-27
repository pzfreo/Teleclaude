"""Pulse — autonomous background agent that periodically checks the user's context.

Triage (cheap, Haiku) decides whether to act; action (full tools, Sonnet) composes
a proactive update for the user.

Runtime-only helpers (Anthropic calls, the tool dispatcher, the tool list builder)
are imported lazily from `bot` inside the functions that need them, to avoid the
circular import that would otherwise occur at module load time.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from persistence import (
    audit_log,
    delete_pulse_goal,
    load_pulse_config,
    load_pulse_goals,
    save_pulse_config,
    save_pulse_goal,
    update_pulse_last_run,
)
from shared import send_long_message

logger = logging.getLogger(__name__)


MANAGE_PULSE_TOOL = {
    "name": "manage_pulse",
    "description": (
        "Configure the autonomous Pulse agent. Pulse periodically reviews your context (calendar, tasks, goals) "
        "and proactively sends helpful updates when something needs attention. Most pulses are silent — "
        "it only messages you when there's something worth knowing. "
        "Use this when the user wants to: add/remove goals for Pulse to watch, enable/disable Pulse, "
        "change check interval, set quiet hours, or check Pulse status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "add_goal",
                    "remove_goal",
                    "list_goals",
                    "enable",
                    "disable",
                    "set_interval",
                    "set_quiet_hours",
                    "status",
                ],
                "description": "The action to perform.",
            },
            "goal": {
                "type": "string",
                "description": "For add_goal: description of what Pulse should watch for.",
            },
            "priority": {
                "type": "string",
                "enum": ["high", "normal", "low"],
                "description": "For add_goal: priority level. Default normal.",
            },
            "goal_id": {
                "type": "integer",
                "description": "For remove_goal: the goal ID to remove.",
            },
            "interval_minutes": {
                "type": "integer",
                "description": "For set_interval: how often to check, in minutes (15-240).",
                "minimum": 15,
                "maximum": 240,
            },
            "quiet_start": {
                "type": "string",
                "description": "For set_quiet_hours: start of quiet period, e.g. '22:00'.",
            },
            "quiet_end": {
                "type": "string",
                "description": "For set_quiet_hours: end of quiet period, e.g. '07:00'.",
            },
        },
        "required": ["action"],
    },
}

# Registered pulse jobs: chat_id -> Job
_pulse_jobs: dict[int, Any] = {}
# In-memory pulse config cache: chat_id -> dict
_pulse_configs: dict[int, dict] = {}

# Pending pulse registrations/unregistrations from sync tool call → picked up by async _process_message
_pending_pulse_registrations: list[int] = []
_pending_pulse_unregistrations: list[int] = []


def _handle_manage_pulse(tool_input: dict, chat_id: int) -> str:
    """Handle the manage_pulse tool call."""
    action = tool_input.get("action", "")

    if action == "add_goal":
        goal_text = tool_input.get("goal", "").strip()
        if not goal_text:
            return "Error: goal text is required."
        priority = tool_input.get("priority", "normal")
        # Ensure config exists
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=60, quiet_start=None, quiet_end=None)
        goal_id = save_pulse_goal(chat_id, goal_text, priority)
        return f"Goal #{goal_id} added: {goal_text} (priority: {priority})"

    if action == "remove_goal":
        remove_id = tool_input.get("goal_id")
        if remove_id is None:
            return "Error: goal_id is required."
        if delete_pulse_goal(int(remove_id), chat_id):
            return f"Goal #{remove_id} removed."
        return f"Goal #{remove_id} not found."

    if action == "list_goals":
        goals = load_pulse_goals(chat_id)
        if not goals:
            return "No pulse goals configured. Add some with add_goal."
        lines = []
        for g in goals:
            lines.append(f"#{g['id']} [{g['priority']}] {g['goal']}")
        return "\n".join(lines)

    if action == "enable":
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=True,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)  # invalidate cache
        _pending_pulse_registrations.append(chat_id)
        goals = load_pulse_goals(chat_id)
        if not goals:
            return "Pulse enabled, but no goals configured yet. Add goals so Pulse knows what to watch."
        return "Pulse enabled. It will start checking on the next interval."

    if action == "disable":
        config = load_pulse_config(chat_id)
        if config:
            save_pulse_config(
                chat_id,
                enabled=False,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        _pending_pulse_unregistrations.append(chat_id)
        return "Pulse disabled."

    if action == "set_interval":
        minutes = tool_input.get("interval_minutes", 60)
        minutes = max(15, min(240, int(minutes)))
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=minutes, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=minutes,
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        if config and config["enabled"]:
            _pending_pulse_registrations.append(chat_id)
        return f"Pulse interval set to {minutes} minutes."

    if action == "set_quiet_hours":
        quiet_start = tool_input.get("quiet_start")
        quiet_end = tool_input.get("quiet_end")
        if not quiet_start or not quiet_end:
            return "Error: both quiet_start and quiet_end are required (e.g. '22:00' and '07:00')."
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=60, quiet_start=quiet_start, quiet_end=quiet_end)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=config["interval_minutes"],
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )
        _pulse_configs.pop(chat_id, None)
        return f"Quiet hours set: {quiet_start} - {quiet_end}."

    if action == "status":
        config = load_pulse_config(chat_id)
        if not config:
            return "Pulse is not configured. Enable it and add goals to get started."
        goals = load_pulse_goals(chat_id)
        lines = [
            f"Enabled: {'yes' if config['enabled'] else 'no'}",
            f"Interval: every {config['interval_minutes']}m",
        ]
        if config["quiet_start"] and config["quiet_end"]:
            lines.append(f"Quiet hours: {config['quiet_start']} - {config['quiet_end']}")
        if config["last_pulse_at"]:
            import bot

            tz = bot._get_user_tz()
            last_dt = datetime.datetime.fromtimestamp(config["last_pulse_at"], tz=tz)
            lines.append(f"Last pulse: {last_dt.strftime('%H:%M %b %d')}")
        lines.append(f"Goals: {len(goals)}")
        for g in goals:
            lines.append(f"  #{g['id']} [{g['priority']}] {g['goal']}")
        return "\n".join(lines)

    return f"Unknown action: {action}"


def _is_quiet_hours(quiet_start: str | None, quiet_end: str | None, tz: datetime.tzinfo) -> bool:
    """Check if current time is within quiet hours."""
    if not quiet_start or not quiet_end:
        return False
    now = datetime.datetime.now(tz)
    try:
        start_h, start_m = (int(x) for x in quiet_start.split(":"))
        end_h, end_m = (int(x) for x in quiet_end.split(":"))
    except (ValueError, AttributeError):
        return False
    current_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes <= end_minutes:
        # Same day: e.g. 09:00 - 17:00
        return start_minutes <= current_minutes < end_minutes
    else:
        # Overnight: e.g. 22:00 - 07:00
        return current_minutes >= start_minutes or current_minutes < end_minutes


async def _build_triage_context(chat_id: int) -> str:
    """Build a compact context snapshot for pulse triage (~500 tokens)."""
    import bot

    parts = []
    loop = asyncio.get_running_loop()

    tz = bot._get_user_tz()
    now = datetime.datetime.now(tz)
    parts.append(f"Time: {now.strftime('%A %H:%M %Z')}")

    # Goals
    goals = load_pulse_goals(chat_id)
    if goals:
        goal_lines = [f"- [{g['priority']}] {g['goal']}" for g in goals]
        parts.append("Goals:\n" + "\n".join(goal_lines))

    # Calendar (next 4 hours)
    if bot.calendar_client and bot.execute_calendar_tool:
        try:
            time_min = now.isoformat()
            time_max = (now + datetime.timedelta(hours=4)).isoformat()
            result = await loop.run_in_executor(
                None,
                bot.execute_calendar_tool,
                bot.calendar_client,
                "list_events",
                {"time_min": time_min, "time_max": time_max, "max_results": 5},
            )
            parts.append(f"Calendar (next 4h): {result[:500]}")
        except Exception as e:
            logger.debug("Pulse triage calendar fetch failed: %s", e)

    # Tasks
    if bot.tasks_client and bot.execute_tasks_tool:
        try:
            result = await loop.run_in_executor(
                None, bot.execute_tasks_tool, bot.tasks_client, "list_tasks", {"max_results": 10}
            )
            parts.append(f"Tasks: {result[:500]}")
        except Exception as e:
            logger.debug("Pulse triage tasks fetch failed: %s", e)

    # Todos
    todos = bot.get_todos(chat_id)
    if todos:
        pending = [t for t in todos if t.get("status") != "completed"]
        if pending:
            parts.append(f"Todos: {bot.format_todo_list(pending)}")

    # Last pulse summary
    config = load_pulse_config(chat_id)
    if config and config.get("last_pulse_summary"):
        parts.append(f"Last pulse said: {config['last_pulse_summary'][:300]}")

    return "\n\n".join(parts)


async def _run_pulse_triage(chat_id: int) -> dict:
    """Run triage with Haiku. Returns {"act": bool, "reason": str}."""
    import bot

    context_text = await _build_triage_context(chat_id)

    triage_prompt = (
        "You are a triage agent for a personal assistant. Review this context snapshot and decide "
        "if there's anything worth proactively telling the user about RIGHT NOW.\n\n"
        "Consider:\n"
        "- Upcoming events they should prepare for\n"
        "- Overdue or urgent tasks\n"
        "- Things matching their stated goals\n"
        "- Time-sensitive information\n\n"
        "Be conservative — silence is better than noise. Only recommend action if there's genuine value.\n"
        "If the last pulse already covered this information, don't repeat it.\n\n"
        f"CONTEXT:\n{context_text}\n\n"
        'Respond with ONLY valid JSON: {"act": true/false, "reason": "brief reason or empty"}'
    )

    try:
        response = await bot._call_anthropic(
            model=bot.BACKGROUND_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": triage_prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        # Parse JSON from response (handle markdown code blocks)
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        return {"act": bool(result.get("act", False)), "reason": result.get("reason", "")}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Pulse triage JSON parse failed: %s — raw: %s", e, text[:200] if "text" in dir() else "n/a")
        return {"act": False, "reason": ""}
    except Exception as e:
        logger.warning("Pulse triage failed: %s", e)
        return {"act": False, "reason": ""}


async def _run_pulse_action(bot_arg, chat_id: int, triage_result: dict) -> None:
    """Run the action phase with Sonnet + full tools. Sends result to user."""
    import bot

    goals = load_pulse_goals(chat_id)
    config = load_pulse_config(chat_id)
    goal_text = "\n".join(f"- [{g['priority']}] {g['goal']}" for g in goals) if goals else "(no specific goals)"
    last_summary = config.get("last_pulse_summary", "") if config else ""

    tz = bot._get_user_tz()
    now = datetime.datetime.now(tz)

    action_prompt = (
        f"You are Teleclaude's Pulse agent running a proactive check at {now.strftime('%H:%M %Z')}.\n\n"
        f"TRIAGE REASON: {triage_result.get('reason', 'General check')}\n\n"
        f"USER'S GOALS:\n{goal_text}\n\n"
        + (f"LAST PULSE SAID: {last_summary[:500]}\n\n" if last_summary else "")
        + "Use the available tools to gather current information relevant to the triage reason and goals. "
        "Then compose a concise, helpful update for the user's phone screen.\n\n"
        "Guidelines:\n"
        "- Be brief — 2-5 lines max unless there's a lot to report.\n"
        "- Don't repeat info from the last pulse unless it has changed.\n"
        "- Actionable > informational.\n"
        "- End with a one-line summary of what you checked (this will be stored as context for next pulse)."
    )

    # Build tools
    tools = bot._build_tool_list()

    system = (
        f"Today is {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({bot.USER_TIMEZONE}).\n\n"
        "You are running as Teleclaude's Pulse agent — a proactive background check. "
        "Keep responses concise and useful for a phone screen."
    )

    repo = bot.get_active_repo(chat_id)
    if repo:
        system += f"\n\nActive repository: {repo}"

    messages: list[dict[str, Any]] = [{"role": "user", "content": action_prompt}]
    loop = asyncio.get_running_loop()

    for _ in range(8):
        response = await bot._call_anthropic(
            model=bot.get_model(chat_id),
            max_tokens=2048,
            system=system,
            messages=messages,
            **({"tools": tools} if tools else {}),
        )

        if response.stop_reason != "tool_use":
            text_parts = [b.text for b in response.content if b.type == "text"]
            reply = "\n".join(text_parts) if text_parts else "(Pulse check completed with no output.)"
            await send_long_message(chat_id, f"Pulse\n\n{reply}", bot_arg, parse_mode="HTML")
            # Store summary (last line or truncated reply)
            summary = reply.split("\n")[-1][:300] if reply else ""
            update_pulse_last_run(chat_id, summary)
            _pulse_configs.pop(chat_id, None)  # invalidate cache
            audit_log("pulse_action", chat_id=chat_id, detail=summary[:100])
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = await loop.run_in_executor(None, bot._execute_tool_call, block, repo, chat_id)
                except Exception as e:
                    result = f"Tool error: {e}"
                result = bot._truncate_result(result)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    await send_long_message(chat_id, "Pulse check hit tool limit.", bot_arg)


async def _run_pulse(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback for pulse checks."""
    import bot

    job_data: dict = context.job.data  # type: ignore[assignment]  # job.data is always a dict in our handlers
    chat_id = job_data["chat_id"]

    # Reload config in case it changed
    config = load_pulse_config(chat_id)
    if not config or not config["enabled"]:
        return

    tz = bot._get_user_tz()

    # Check quiet hours
    if _is_quiet_hours(config.get("quiet_start"), config.get("quiet_end"), tz):
        logger.debug("Pulse for chat %d skipped — quiet hours", chat_id)
        return

    # Check goals exist
    goals = load_pulse_goals(chat_id)
    if not goals:
        logger.debug("Pulse for chat %d skipped — no goals", chat_id)
        return

    logger.info("Pulse triage starting for chat %d", chat_id)

    # Phase 1: Triage (cheap)
    triage = await _run_pulse_triage(chat_id)
    if not triage.get("act"):
        logger.info("Pulse triage for chat %d: no action needed", chat_id)
        return

    # Phase 2: Action (full tools)
    logger.info("Pulse action for chat %d: %s", chat_id, triage.get("reason", "")[:80])
    try:
        await _run_pulse_action(context.bot, chat_id, triage)
    except Exception as e:
        logger.warning("Pulse action failed for chat %d: %s", chat_id, e)


def _register_pulse(job_queue, chat_id: int) -> None:
    """Register a pulse job for a chat."""
    # Unregister existing first
    _unregister_pulse(chat_id)

    config = load_pulse_config(chat_id)
    if not config or not config["enabled"]:
        return

    interval = config["interval_minutes"] * 60
    job_data = {"chat_id": chat_id}
    job_name = f"pulse_{chat_id}"

    job = job_queue.run_repeating(
        _run_pulse,
        interval=interval,
        first=30,  # first check 30s after registration
        data=job_data,
        name=job_name,
    )
    _pulse_jobs[chat_id] = job
    logger.info("Registered pulse for chat %d: every %dm", chat_id, config["interval_minutes"])


def _unregister_pulse(chat_id: int) -> None:
    """Remove a pulse job from the job queue."""
    job = _pulse_jobs.pop(chat_id, None)
    if job:
        job.schedule_removal()


async def pulse_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pulse command handler."""
    import bot

    if not bot.is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    args = context.args or []

    if not args:
        # Show status
        config = load_pulse_config(chat_id)
        goals = load_pulse_goals(chat_id)
        if not config:
            await update.message.reply_text(
                "Pulse is not configured yet.\n\n"
                "Pulse is an autonomous agent that periodically checks your context "
                "(calendar, tasks, goals) and sends updates when something needs attention.\n\n"
                "Get started:\n"
                "/pulse on — enable Pulse\n"
                "Then tell me what to watch for, e.g.:\n"
                '"Keep an eye on my PRs"\n'
                '"Remind me about overdue tasks"\n'
                '"Watch for calendar conflicts"'
            )
            return

        lines = [f"Pulse: {'ON' if config['enabled'] else 'OFF'}"]
        lines.append(f"Interval: every {config['interval_minutes']}m")
        if config["quiet_start"] and config["quiet_end"]:
            lines.append(f"Quiet hours: {config['quiet_start']} - {config['quiet_end']}")
        if config.get("last_pulse_at"):
            tz = bot._get_user_tz()
            last_dt = datetime.datetime.fromtimestamp(config["last_pulse_at"], tz=tz)
            lines.append(f"Last active pulse: {last_dt.strftime('%H:%M %b %d')}")

        if goals:
            lines.append(f"\nGoals ({len(goals)}):")
            for g in goals:
                lines.append(f"  #{g['id']} [{g['priority']}] {g['goal']}")
        else:
            lines.append("\nNo goals. Tell me what to watch for.")

        await update.message.reply_text("\n".join(lines))
        return

    subcmd = args[0].lower()

    if subcmd in ("on", "enable"):
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=True, interval_minutes=60, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=True,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        if context.application.job_queue:
            _register_pulse(context.application.job_queue, chat_id)
        goals = load_pulse_goals(chat_id)
        if goals:
            await update.message.reply_text("Pulse enabled.")
        else:
            await update.message.reply_text("Pulse enabled. Now tell me what to watch for.")
        return

    if subcmd in ("off", "disable"):
        config = load_pulse_config(chat_id)
        if config:
            save_pulse_config(
                chat_id,
                enabled=False,
                interval_minutes=config["interval_minutes"],
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        _unregister_pulse(chat_id)
        await update.message.reply_text("Pulse disabled.")
        return

    if subcmd == "every":
        if len(args) < 2:
            await update.message.reply_text("Usage: /pulse every 30m or /pulse every 2h")
            return
        interval_str = args[1].lower()
        match = re.match(r"^(\d+)(m|h)$", interval_str)
        if not match:
            await update.message.reply_text("Invalid interval. Use e.g. 30m or 2h.")
            return
        value = int(match.group(1))
        unit = match.group(2)
        minutes = value if unit == "m" else value * 60
        minutes = max(15, min(240, minutes))
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=minutes, quiet_start=None, quiet_end=None)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=minutes,
                quiet_start=config["quiet_start"],
                quiet_end=config["quiet_end"],
            )
        _pulse_configs.pop(chat_id, None)
        if config and config["enabled"] and context.application.job_queue:
            _register_pulse(context.application.job_queue, chat_id)
        await update.message.reply_text(f"Pulse interval set to {minutes} minutes.")
        return

    if subcmd == "quiet":
        if len(args) < 2:
            await update.message.reply_text("Usage: /pulse quiet 22:00-07:00")
            return
        time_range = args[1]
        parts = time_range.split("-")
        if len(parts) != 2:
            await update.message.reply_text("Usage: /pulse quiet 22:00-07:00")
            return
        quiet_start, quiet_end = parts[0].strip(), parts[1].strip()
        config = load_pulse_config(chat_id)
        if not config:
            save_pulse_config(chat_id, enabled=False, interval_minutes=60, quiet_start=quiet_start, quiet_end=quiet_end)
        else:
            save_pulse_config(
                chat_id,
                enabled=config["enabled"],
                interval_minutes=config["interval_minutes"],
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )
        _pulse_configs.pop(chat_id, None)
        await update.message.reply_text(f"Quiet hours set: {quiet_start} - {quiet_end}")
        return

    if subcmd == "goals":
        goals = load_pulse_goals(chat_id)
        if not goals:
            await update.message.reply_text("No goals. Tell me what to watch for.")
        else:
            lines = [f"#{g['id']} [{g['priority']}] {g['goal']}" for g in goals]
            await update.message.reply_text("\n".join(lines))
        return

    if subcmd == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /pulse remove <goal_id>")
            return
        try:
            goal_id = int(args[1].lstrip("#"))
        except ValueError:
            await update.message.reply_text("Invalid goal ID.")
            return
        if delete_pulse_goal(goal_id, chat_id):
            await update.message.reply_text(f"Goal #{goal_id} removed.")
        else:
            await update.message.reply_text(f"Goal #{goal_id} not found.")
        return

    await update.message.reply_text(
        "Usage:\n"
        "/pulse — show status\n"
        "/pulse on/off — enable/disable\n"
        "/pulse every 30m — set interval\n"
        "/pulse quiet 22:00-07:00 — set quiet hours\n"
        "/pulse goals — list goals\n"
        "/pulse remove <id> — remove a goal"
    )
