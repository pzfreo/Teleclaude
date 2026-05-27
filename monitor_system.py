"""Monitor system — recurring background checks with change detection.

Tools and JobQueue glue for the `schedule_check` Anthropic tool. Each monitor
periodically runs a `check_prompt` through the tool loop, compares the result
against the previous snapshot via Claude, and notifies the user when a
`notify_condition` is satisfied.

Runtime helpers from bot.py (Anthropic, tool dispatch, model, repo) are
imported lazily inside the functions that need them to avoid a circular
import at module load time.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

from telegram.ext import ContextTypes

from persistence import (
    audit_log,
    count_monitors,
    disable_monitor,
    save_monitor,
    update_monitor_result,
)
from shared import send_long_message

logger = logging.getLogger(__name__)


SCHEDULE_CHECK_TOOL = {
    "name": "schedule_check",
    "description": (
        "Schedule a recurring background check that monitors something and notifies the user "
        "when conditions change. Use this when the user wants to be alerted about future changes "
        "(e.g. train delays, new GitHub issues, PR merges). The check runs every interval_minutes "
        "until expires_at, using all available tools to gather current state. It compares each "
        "result with the previous one and only notifies the user if the notify_condition is met. "
        "First run captures a baseline silently. Auto-expires — max 24 hours. Max 5 active monitors."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "check_prompt": {
                "type": "string",
                "description": (
                    "The prompt to run each check cycle. Should instruct use of specific tools "
                    "(e.g. get_train_departures, list_issues). Be specific about what data to gather."
                ),
            },
            "notify_condition": {
                "type": "string",
                "description": (
                    "When to notify the user. Describe the change that matters. "
                    "E.g. 'Any train is delayed by more than 5 minutes or cancelled', "
                    "'A new issue was opened', 'The CI check failed'"
                ),
            },
            "interval_minutes": {
                "type": "integer",
                "description": "How often to check, in minutes. Min 5, max 60. Use 5-10 for trains, 15-30 for GitHub.",
                "minimum": 5,
                "maximum": 60,
            },
            "expires_at": {
                "type": "string",
                "description": (
                    "ISO 8601 datetime when monitoring should stop. Max 24h from now. "
                    "Choose sensibly: train monitoring until journey time, GitHub until end of day."
                ),
            },
            "summary": {
                "type": "string",
                "description": "Brief human-readable label, e.g. 'CHI→VIC train delays', 'New issues on Teleclaude'",
            },
        },
        "required": ["check_prompt", "notify_condition", "interval_minutes", "expires_at", "summary"],
    },
}

MAX_MONITORS_PER_CHAT = 5
MAX_MONITOR_DURATION_HOURS = 24

# Registered monitor jobs: monitor_id -> Job
_monitor_jobs: dict[int, Any] = {}
# Pending registrations from sync _execute_tool_call → picked up by async _process_message
_pending_monitor_registrations: list[dict] = []


def _handle_schedule_check(tool_input: dict, chat_id: int) -> str:
    """Handle the schedule_check tool call — validate and persist a new monitor."""
    import bot

    # Validate limits
    active_count = count_monitors(chat_id)
    if active_count >= MAX_MONITORS_PER_CHAT:
        return f"Monitor limit reached ({MAX_MONITORS_PER_CHAT} active). Remove one with /monitors remove <id> first."

    check_prompt = tool_input.get("check_prompt", "")
    notify_condition = tool_input.get("notify_condition", "")
    interval_minutes = tool_input.get("interval_minutes", 10)
    expires_at_str = tool_input.get("expires_at", "")
    summary = tool_input.get("summary", "Monitor")

    if not check_prompt or not notify_condition:
        return "Error: check_prompt and notify_condition are required."

    interval_minutes = max(5, min(60, int(interval_minutes)))

    # Parse expiry
    tz = bot._get_user_tz()

    now = datetime.datetime.now(tz)
    try:
        expires_dt = datetime.datetime.fromisoformat(expires_at_str)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=tz)
    except (ValueError, TypeError):
        # Default: 2 hours from now
        expires_dt = now + datetime.timedelta(hours=2)

    # Cap at 24 hours
    max_expiry = now + datetime.timedelta(hours=MAX_MONITOR_DURATION_HOURS)
    if expires_dt > max_expiry:
        expires_dt = max_expiry
    if expires_dt <= now:
        return "Error: expires_at must be in the future."

    expires_at_ts = expires_dt.timestamp()

    monitor_id = save_monitor(
        chat_id=chat_id,
        check_prompt=check_prompt,
        notify_condition=notify_condition,
        interval_minutes=interval_minutes,
        expires_at=expires_at_ts,
        summary=summary,
    )

    # Register with job queue (needs to happen on the event loop)
    # Store pending registration — picked up by the async tool loop in _process_message
    _pending_monitor_registrations.append(
        {
            "id": monitor_id,
            "chat_id": chat_id,
            "check_prompt": check_prompt,
            "notify_condition": notify_condition,
            "interval_minutes": interval_minutes,
            "expires_at": expires_at_ts,
            "summary": summary,
            "last_result": None,
        }
    )

    expires_str = (
        expires_dt.strftime("%H:%M %Z") if expires_dt.date() == now.date() else expires_dt.strftime("%b %d %H:%M")
    )
    audit_log("monitor_created", chat_id=chat_id, detail=f"#{monitor_id}: {summary}")
    return (
        f"Monitor #{monitor_id} created: {summary}\n"
        f"Checking every {interval_minutes}m until {expires_str}.\n"
        f"First check will run shortly to capture a baseline."
    )


def _register_monitor(job_queue, monitor: dict, bot=None) -> None:
    """Register a monitor dict as a repeating JobQueue job."""
    monitor_id = monitor["id"]
    job_data = {**monitor, "bot": bot}
    job_name = f"monitor_{monitor_id}"
    interval = monitor["interval_minutes"] * 60

    job = job_queue.run_repeating(
        _run_monitor_job,
        interval=interval,
        first=10,  # first check 10s after creation
        data=job_data,
        name=job_name,
    )
    _monitor_jobs[monitor_id] = job
    logger.info("Registered monitor #%d: every %dm — %s", monitor_id, monitor["interval_minutes"], monitor["summary"])


def _unregister_monitor(monitor_id: int) -> None:
    """Remove a monitor job from the job queue."""
    job = _monitor_jobs.pop(monitor_id, None)
    if job:
        job.schedule_removal()


async def _run_monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback for monitor checks."""
    job_data: dict = context.job.data  # type: ignore[assignment]  # job.data is always a dict in our handlers
    monitor_id = job_data["id"]
    chat_id = job_data["chat_id"]
    check_prompt = job_data["check_prompt"]
    notify_condition = job_data["notify_condition"]
    summary = job_data["summary"]
    expires_at = job_data["expires_at"]
    last_result = job_data.get("last_result")

    # Check expiry
    if time.time() > expires_at:
        _unregister_monitor(monitor_id)
        disable_monitor(monitor_id)
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"Monitor expired: {summary}")
        except Exception:
            pass
        logger.info("Monitor #%d expired: %s", monitor_id, summary)
        return

    try:
        # Run the check prompt through the tool loop to get current state
        current_result = await _run_monitor_prompt(context.bot, chat_id, check_prompt)

        if last_result is None:
            # First run — capture baseline silently
            job_data["last_result"] = current_result
            update_monitor_result(monitor_id, current_result)
            logger.info("Monitor #%d baseline captured", monitor_id)
            return

        # Compare old vs new
        should_notify, alert_msg = await _compare_monitor_results(
            last_result, current_result, notify_condition, summary
        )

        # Update stored result
        job_data["last_result"] = current_result
        update_monitor_result(monitor_id, current_result)

        if should_notify and alert_msg:
            await send_long_message(chat_id, f"🔔 {summary}\n\n{alert_msg}", context.bot, parse_mode="HTML")
            audit_log("monitor_alert", chat_id=chat_id, detail=f"#{monitor_id}: {summary}")

    except Exception as e:
        logger.warning("Monitor #%d check failed: %s", monitor_id, e)


async def _run_monitor_prompt(bot_arg, chat_id: int, prompt: str) -> str:
    """Run a monitor check prompt through the tool loop, returning the text result.

    Similar to run_scheduled_prompt but returns text instead of sending it.
    """
    import bot

    tools = bot._build_tool_list(include_email=False)

    tz = bot._get_user_tz()
    now = datetime.datetime.now(tz)

    system = (
        f"Today is {now.strftime('%A, %B %d, %Y at %I:%M %p')} ({bot.USER_TIMEZONE}).\n\n"
        "You are running a background monitoring check. Gather the requested data using the "
        "available tools and return a concise factual summary of the current state. "
        "Do NOT address the user — just report the data."
    )

    repo = bot.get_active_repo(chat_id)
    if repo:
        system += f"\n\nActive repository: {repo}"

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    loop = asyncio.get_running_loop()

    for _ in range(5):  # fewer rounds than interactive — monitors should be quick
        response = await bot._call_anthropic(
            model=bot.BACKGROUND_MODEL,  # use Haiku for cost efficiency
            max_tokens=1024,
            system=system,
            messages=messages,
            **({"tools": tools} if tools else {}),
        )

        if response.stop_reason != "tool_use":
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts) if text_parts else "(no data)"

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

    return "(monitor check hit tool limit)"


async def _compare_monitor_results(
    previous: str, current: str, notify_condition: str, summary: str
) -> tuple[bool, str | None]:
    """Use Claude to compare two monitor snapshots and decide whether to notify.

    Returns (should_notify, alert_message_or_none).
    """
    import bot

    prompt = (
        f"You are comparing two snapshots from a background monitor: '{summary}'.\n\n"
        f"PREVIOUS STATE:\n{previous[:3000]}\n\n"
        f"CURRENT STATE:\n{current[:3000]}\n\n"
        f"NOTIFY CONDITION: {notify_condition}\n\n"
        "Does the current state meet the notify condition (compared to the previous state)?\n"
        "If YES: write a concise alert message for a phone notification (2-3 lines max).\n"
        "If NO: respond with exactly the word NO_CHANGE and nothing else."
    )

    response = await bot._call_anthropic(
        model=bot.BACKGROUND_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    text_parts = [b.text for b in response.content if b.type == "text"]
    reply = "\n".join(text_parts).strip()

    if reply == "NO_CHANGE" or reply.startswith("NO_CHANGE"):
        return False, None
    return True, reply
