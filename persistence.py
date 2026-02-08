"""SQLite persistence for conversation history and per-chat state."""

import json
import logging
import sqlite3
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
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            chat_id INTEGER PRIMARY KEY,
            messages TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS active_repos (
            chat_id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL
        );
        """
    )
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


def load_conversation(chat_id: int) -> list[dict]:
    """Load conversation history for a chat."""
    conn = _connect()
    row = conn.execute(
        "SELECT messages FROM conversations WHERE chat_id = ?", (chat_id,)
    ).fetchone()
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
    row = conn.execute(
        "SELECT repo FROM active_repos WHERE chat_id = ?", (chat_id,)
    ).fetchone()
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


def _serialize(obj):
    """JSON serializer for Anthropic content block objects."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
