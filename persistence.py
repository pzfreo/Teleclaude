"""SQLite persistence for conversation history and per-chat state."""

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "teleclaude.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            chat_id INTEGER PRIMARY KEY,
            messages TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS active_repos (
            chat_id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS todo_lists (
            chat_id INTEGER PRIMARY KEY,
            todos TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS chat_modes (
            chat_id INTEGER PRIMARY KEY,
            plan_mode INTEGER NOT NULL DEFAULT 0,
            agent_mode INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            chat_id INTEGER,
            user_id INTEGER,
            event TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_log_chat_id ON audit_log(chat_id);
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            interval_type TEXT NOT NULL,
            interval_value TEXT NOT NULL,
            prompt TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_schedules_chat_id ON schedules(chat_id);
        CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            check_prompt TEXT NOT NULL,
            notify_condition TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            summary TEXT NOT NULL,
            last_result TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_monitors_chat_id ON monitors(chat_id);
        CREATE TABLE IF NOT EXISTS pulse_config (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            interval_minutes INTEGER NOT NULL DEFAULT 60,
            quiet_start TEXT,
            quiet_end TEXT,
            last_pulse_at REAL,
            last_pulse_summary TEXT,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pulse_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            goal TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal',
            created_at REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_pulse_goals_chat_id ON pulse_goals(chat_id);
        """)
    # Migrations for existing databases
    _migrate(conn)
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that may not exist in older databases."""
    cursor = conn.execute("PRAGMA table_info(chat_modes)")
    columns = {row[1] for row in cursor.fetchall()}
    if "agent_mode" not in columns:
        conn.execute("ALTER TABLE chat_modes ADD COLUMN agent_mode INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        logger.info("Migrated chat_modes: added agent_mode column")
    if "model" not in columns:
        conn.execute("ALTER TABLE chat_modes ADD COLUMN model TEXT")
        conn.commit()
        logger.info("Migrated chat_modes: added model column")

    # Check active_repos for branch column
    cursor = conn.execute("PRAGMA table_info(active_repos)")
    repo_columns = {row[1] for row in cursor.fetchall()}
    if "branch" not in repo_columns:
        conn.execute("ALTER TABLE active_repos ADD COLUMN branch TEXT")
        conn.commit()
        logger.info("Migrated active_repos: added branch column")
    if "session_id" not in repo_columns:
        conn.execute("ALTER TABLE active_repos ADD COLUMN session_id TEXT")
        conn.commit()
        logger.info("Migrated active_repos: added session_id column")


def load_conversation(chat_id: int) -> list[dict]:
    """Load conversation history for a chat."""
    conn = _connect()
    row = conn.execute("SELECT messages FROM conversations WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    if row:
        return json.loads(row[0])
    return []


def save_conversation(chat_id: int, messages: list) -> None:
    """Save conversation history for a chat."""
    conn = _connect()
    # Serialize — handles both dicts and Anthropic content block objects
    serialized = json.dumps(messages, default=_serialize)
    conn.execute(
        "INSERT OR REPLACE INTO conversations (chat_id, messages) VALUES (?, ?)",
        (chat_id, serialized),
    )
    conn.commit()
    conn.close()


def clear_conversation(chat_id: int) -> None:
    """Clear conversation history for a chat."""
    conn = _connect()
    conn.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def load_active_repo(chat_id: int) -> str | None:
    """Load active repo for a chat."""
    conn = _connect()
    row = conn.execute("SELECT repo FROM active_repos WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def save_active_repo(chat_id: int, repo: str) -> None:
    """Save active repo for a chat."""
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO active_repos (chat_id, repo) VALUES (?, ?)",
        (chat_id, repo),
    )
    conn.commit()
    conn.close()


def load_active_branch(chat_id: int) -> str | None:
    conn = _connect()
    row = conn.execute("SELECT branch FROM active_repos WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def save_active_branch(chat_id: int, branch: str | None) -> None:
    conn = _connect()
    conn.execute(
        """UPDATE active_repos SET branch = ? WHERE chat_id = ?""",
        (branch, chat_id),
    )
    conn.commit()
    conn.close()


def load_session_id(chat_id: int) -> str | None:
    """Load persisted CLI session ID for a chat."""
    conn = _connect()
    row = conn.execute("SELECT session_id FROM active_repos WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def save_session_id(chat_id: int, session_id: str | None) -> None:
    """Persist CLI session ID for a chat (requires active_repos row to exist)."""
    conn = _connect()
    conn.execute("UPDATE active_repos SET session_id = ? WHERE chat_id = ?", (session_id, chat_id))
    conn.commit()
    conn.close()


def load_todos(chat_id: int) -> list[dict]:
    conn = _connect()
    row = conn.execute("SELECT todos FROM todo_lists WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else []


def save_todos(chat_id: int, todos: list[dict]) -> None:
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO todo_lists (chat_id, todos) VALUES (?, ?)",
        (chat_id, json.dumps(todos)),
    )
    conn.commit()
    conn.close()


def load_plan_mode(chat_id: int) -> bool:
    conn = _connect()
    row = conn.execute("SELECT plan_mode FROM chat_modes WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return bool(row[0]) if row else False


def save_plan_mode(chat_id: int, enabled: bool) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO chat_modes (chat_id, plan_mode) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET plan_mode = excluded.plan_mode""",
        (chat_id, int(enabled)),
    )
    conn.commit()
    conn.close()


def load_agent_mode(chat_id: int) -> bool:
    conn = _connect()
    row = conn.execute("SELECT agent_mode FROM chat_modes WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return bool(row[0]) if row else False


def save_agent_mode(chat_id: int, enabled: bool) -> None:
    conn = _connect()
    # Use upsert to avoid overwriting plan_mode
    conn.execute(
        """INSERT INTO chat_modes (chat_id, agent_mode) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET agent_mode = excluded.agent_mode""",
        (chat_id, int(enabled)),
    )
    conn.commit()
    conn.close()


def load_model(chat_id: int) -> str | None:
    """Load persisted model choice for a chat. Returns None if not set."""
    conn = _connect()
    row = conn.execute("SELECT model FROM chat_modes WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def save_model(chat_id: int, model: str) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO chat_modes (chat_id, model) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET model = excluded.model""",
        (chat_id, model),
    )
    conn.commit()
    conn.close()


# ── Schedules ────────────────────────────────────────────────────────


def load_schedules(chat_id: int) -> list[dict]:
    """Load all enabled schedules for a chat."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, chat_id, interval_type, interval_value, prompt, enabled, created_at "
        "FROM schedules WHERE chat_id = ? AND enabled = 1 ORDER BY id",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "chat_id": r[1],
            "interval_type": r[2],
            "interval_value": r[3],
            "prompt": r[4],
            "enabled": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def load_all_schedules() -> list[dict]:
    """Load all enabled schedules across all chats."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, chat_id, interval_type, interval_value, prompt, enabled, created_at "
        "FROM schedules WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "chat_id": r[1],
            "interval_type": r[2],
            "interval_value": r[3],
            "prompt": r[4],
            "enabled": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def save_schedule(chat_id: int, interval_type: str, interval_value: str, prompt: str) -> int:
    """Create a new schedule. Returns the new schedule ID."""
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO schedules (chat_id, interval_type, interval_value, prompt, enabled, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (chat_id, interval_type, interval_value, prompt, time.time()),
    )
    schedule_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return schedule_id  # type: ignore[return-value]


def delete_schedule(schedule_id: int, chat_id: int) -> bool:
    """Delete a schedule by ID, scoped to a chat. Returns True if a row was deleted."""
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM schedules WHERE id = ? AND chat_id = ?",
        (schedule_id, chat_id),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# ── Monitors ────────────────────────────────────────────────────────


def save_monitor(
    chat_id: int,
    check_prompt: str,
    notify_condition: str,
    interval_minutes: int,
    expires_at: float,
    summary: str,
) -> int:
    """Create a new monitor. Returns the new monitor ID."""
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO monitors (chat_id, check_prompt, notify_condition, interval_minutes, "
        "expires_at, summary, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (chat_id, check_prompt, notify_condition, interval_minutes, expires_at, summary, time.time()),
    )
    monitor_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return monitor_id  # type: ignore[return-value]


def load_monitors(chat_id: int) -> list[dict]:
    """Load all enabled monitors for a chat."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, chat_id, check_prompt, notify_condition, interval_minutes, "
        "expires_at, summary, last_result, enabled, created_at "
        "FROM monitors WHERE chat_id = ? AND enabled = 1 ORDER BY id",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [_monitor_row_to_dict(r) for r in rows]


def load_all_monitors() -> list[dict]:
    """Load all enabled monitors across all chats."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, chat_id, check_prompt, notify_condition, interval_minutes, "
        "expires_at, summary, last_result, enabled, created_at "
        "FROM monitors WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    conn.close()
    return [_monitor_row_to_dict(r) for r in rows]


def count_monitors(chat_id: int) -> int:
    """Count active monitors for a chat."""
    conn = _connect()
    row = conn.execute("SELECT COUNT(*) FROM monitors WHERE chat_id = ? AND enabled = 1", (chat_id,)).fetchone()
    conn.close()
    return row[0] if row else 0


def update_monitor_result(monitor_id: int, result: str) -> None:
    """Update the last_result for a monitor."""
    conn = _connect()
    conn.execute("UPDATE monitors SET last_result = ? WHERE id = ?", (result, monitor_id))
    conn.commit()
    conn.close()


def delete_monitor(monitor_id: int, chat_id: int) -> bool:
    """Delete a monitor by ID, scoped to a chat. Returns True if a row was deleted."""
    conn = _connect()
    cursor = conn.execute("DELETE FROM monitors WHERE id = ? AND chat_id = ?", (monitor_id, chat_id))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def disable_monitor(monitor_id: int) -> None:
    """Disable a monitor (soft delete for expiry)."""
    conn = _connect()
    conn.execute("UPDATE monitors SET enabled = 0 WHERE id = ?", (monitor_id,))
    conn.commit()
    conn.close()


def _monitor_row_to_dict(r) -> dict:
    return {
        "id": r[0],
        "chat_id": r[1],
        "check_prompt": r[2],
        "notify_condition": r[3],
        "interval_minutes": r[4],
        "expires_at": r[5],
        "summary": r[6],
        "last_result": r[7],
        "enabled": r[8],
        "created_at": r[9],
    }


# ── Pulse ─────────────────────────────────────────────────────────────


def load_pulse_config(chat_id: int) -> dict | None:
    """Load pulse config for a chat. Returns None if not configured."""
    conn = _connect()
    row = conn.execute(
        "SELECT chat_id, enabled, interval_minutes, quiet_start, quiet_end, "
        "last_pulse_at, last_pulse_summary, created_at FROM pulse_config WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "chat_id": row[0],
        "enabled": bool(row[1]),
        "interval_minutes": row[2],
        "quiet_start": row[3],
        "quiet_end": row[4],
        "last_pulse_at": row[5],
        "last_pulse_summary": row[6],
        "created_at": row[7],
    }


def save_pulse_config(
    chat_id: int, enabled: bool, interval_minutes: int, quiet_start: str | None, quiet_end: str | None
) -> None:
    """Create or update pulse config for a chat."""
    conn = _connect()
    conn.execute(
        "INSERT INTO pulse_config (chat_id, enabled, interval_minutes, quiet_start, quiet_end, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled, interval_minutes=excluded.interval_minutes, "
        "quiet_start=excluded.quiet_start, quiet_end=excluded.quiet_end",
        (chat_id, int(enabled), interval_minutes, quiet_start, quiet_end, time.time()),
    )
    conn.commit()
    conn.close()


def update_pulse_last_run(chat_id: int, summary: str) -> None:
    """Update last pulse run time and summary."""
    conn = _connect()
    conn.execute(
        "UPDATE pulse_config SET last_pulse_at = ?, last_pulse_summary = ? WHERE chat_id = ?",
        (time.time(), summary, chat_id),
    )
    conn.commit()
    conn.close()


def load_pulse_goals(chat_id: int) -> list[dict]:
    """Load all enabled pulse goals for a chat."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, chat_id, goal, priority, created_at, enabled "
        "FROM pulse_goals WHERE chat_id = ? AND enabled = 1 ORDER BY id",
        (chat_id,),
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "chat_id": r[1], "goal": r[2], "priority": r[3], "created_at": r[4], "enabled": bool(r[5])}
        for r in rows
    ]


def save_pulse_goal(chat_id: int, goal: str, priority: str = "normal") -> int:
    """Create a new pulse goal. Returns the goal ID."""
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO pulse_goals (chat_id, goal, priority, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, goal, priority, time.time()),
    )
    goal_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return goal_id  # type: ignore[return-value]


def delete_pulse_goal(goal_id: int, chat_id: int) -> bool:
    """Delete a pulse goal by ID, scoped to a chat. Returns True if deleted."""
    conn = _connect()
    cursor = conn.execute("DELETE FROM pulse_goals WHERE id = ? AND chat_id = ?", (goal_id, chat_id))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def load_all_pulse_configs() -> list[dict]:
    """Load all enabled pulse configs across all chats."""
    conn = _connect()
    rows = conn.execute(
        "SELECT chat_id, enabled, interval_minutes, quiet_start, quiet_end, "
        "last_pulse_at, last_pulse_summary, created_at FROM pulse_config WHERE enabled = 1"
    ).fetchall()
    conn.close()
    return [
        {
            "chat_id": r[0],
            "enabled": bool(r[1]),
            "interval_minutes": r[2],
            "quiet_start": r[3],
            "quiet_end": r[4],
            "last_pulse_at": r[5],
            "last_pulse_summary": r[6],
            "created_at": r[7],
        }
        for r in rows
    ]


def _serialize(obj):
    """JSON serializer for Anthropic content block objects.

    Uses exclude_none to strip SDK-internal fields (e.g. parsed_output)
    that the API rejects when sent back in conversation history.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=True)
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# ── Audit logging ────────────────────────────────────────────────────


def audit_log(event: str, *, chat_id: int | None = None, user_id: int | None = None, detail: str = "") -> None:
    """Write a structured audit log entry to the database."""
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO audit_log (timestamp, chat_id, user_id, event, detail) VALUES (?, ?, ?, ?, ?)",
            (time.time(), chat_id, user_id, event, detail),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("Audit log write failed", exc_info=True)


def get_audit_log(limit: int = 100, chat_id: int | None = None) -> list[dict]:
    """Read recent audit log entries."""
    conn = _connect()
    if chat_id is not None:
        rows = conn.execute(
            "SELECT id, timestamp, chat_id, user_id, event, detail FROM audit_log WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, timestamp, chat_id, user_id, event, detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {"id": r[0], "timestamp": r[1], "chat_id": r[2], "user_id": r[3], "event": r[4], "detail": r[5]} for r in rows
    ]
