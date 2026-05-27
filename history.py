"""Conversation history cache, sanitization, and trimming.

Holds the in-memory `conversations` cache plus the helpers that keep history
valid for the Anthropic API (matched tool_use/tool_result pairs, no thinking
blocks) and bounded in size/content.
"""

from __future__ import annotations

import logging
from typing import Any

from persistence import load_conversation, save_conversation

logger = logging.getLogger(__name__)

# In-memory conversation cache (backed by SQLite via persistence)
conversations: dict[int, list] = {}

MAX_HISTORY = 50
MAX_CONTENT_SIZE = 20000  # max chars per content string in history
# Number of recent messages to keep images for (the rest get stripped)
_KEEP_IMAGES_LAST_N = 10


def _trim_content(content, keep_images: bool = True) -> Any:
    """Truncate oversized content blocks when reloading history.

    When keep_images is False, replace image/document blocks with text placeholders
    to save context space for older messages.
    """
    if isinstance(content, str) and len(content) > MAX_CONTENT_SIZE:
        return content[:MAX_CONTENT_SIZE] + "\n... (truncated)"
    if isinstance(content, list):
        trimmed = []
        for item in content:
            if isinstance(item, dict):
                item = dict(item)  # shallow copy
                # Strip binary data from old messages
                if not keep_images and item.get("type") == "image":
                    trimmed.append({"type": "text", "text": "[image was here]"})
                    continue
                if not keep_images and item.get("type") == "document":
                    trimmed.append({"type": "text", "text": "[document was here]"})
                    continue
                if isinstance(item.get("content"), str) and len(item["content"]) > MAX_CONTENT_SIZE:
                    item["content"] = item["content"][:MAX_CONTENT_SIZE] + "\n... (truncated)"
            trimmed.append(item)
        return trimmed
    return content


def _sanitize_history(history: list[dict]) -> list[dict]:
    """Ensure history is valid for the Anthropic API.

    - Every tool_use block must have a matching tool_result in the next message.
    - History must start with a user message.
    - Remove thinking blocks (they cause issues when sent back).
    """
    if not history:
        return history

    # Clean assistant content blocks:
    # - Convert SDK objects to plain dicts (SDK objects bypass isinstance(b, dict) checks below)
    # - Strip thinking blocks (they can't be replayed)
    # - Remove SDK-internal fields like parsed_output that the API rejects
    _KNOWN_TEXT_KEYS = {"type", "text"}
    _KNOWN_TOOL_USE_KEYS = {"type", "id", "name", "input"}
    for msg in history:
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            cleaned = []
            for b in msg["content"]:
                # Convert SDK objects (e.g. ToolUseBlock, TextBlock) to plain dicts
                if not isinstance(b, dict) and hasattr(b, "model_dump"):
                    b = b.model_dump(exclude_none=True)
                elif not isinstance(b, dict) and hasattr(b, "__dict__"):
                    b = dict(b.__dict__)
                if not isinstance(b, dict):
                    continue  # Skip non-dict, non-SDK items we can't process
                if b.get("type") == "thinking":
                    continue
                if b.get("type") == "text":
                    cleaned.append({k: v for k, v in b.items() if k in _KNOWN_TEXT_KEYS})
                elif b.get("type") == "tool_use":
                    cleaned.append({k: v for k, v in b.items() if k in _KNOWN_TOOL_USE_KEYS})
                else:
                    cleaned.append(b)
            msg["content"] = cleaned
        # Also convert SDK objects in user messages (tool_result blocks)
        elif msg.get("role") == "user" and isinstance(msg.get("content"), list):
            cleaned = []
            for b in msg["content"]:
                if not isinstance(b, dict) and hasattr(b, "model_dump"):
                    b = b.model_dump(exclude_none=True)
                elif not isinstance(b, dict) and hasattr(b, "__dict__"):
                    b = dict(b.__dict__)
                if isinstance(b, dict):
                    cleaned.append(b)
            if cleaned:
                msg["content"] = cleaned

    # Walk forward and remove orphaned tool_use/tool_result pairs
    sanitized = []
    i = 0
    while i < len(history):
        msg = history[i]

        # Check if this assistant message has tool_use blocks
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            tool_use_ids = {
                b["id"] for b in msg["content"] if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b
            }

            if tool_use_ids:
                # There must be a next message with matching tool_results
                if i + 1 < len(history):
                    next_msg = history[i + 1]
                    if next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
                        result_ids = {
                            b.get("tool_use_id")
                            for b in next_msg["content"]
                            if isinstance(b, dict) and b.get("type") == "tool_result"
                        }
                        if tool_use_ids == result_ids:
                            # Pair is complete, keep both
                            sanitized.append(msg)
                            sanitized.append(next_msg)
                            i += 2
                            continue
                # Pair is broken — skip the assistant message
                logger.warning("Dropping orphaned tool_use message at index %d", i)
                # Also skip the next message if it contains orphaned tool_results
                if i + 1 < len(history):
                    next_msg = history[i + 1]
                    if next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
                        has_tool_results = any(
                            isinstance(b, dict) and b.get("type") == "tool_result" for b in next_msg["content"]
                        )
                        if has_tool_results:
                            # Strip tool_result blocks, keep any other content (e.g. text)
                            kept = [
                                b
                                for b in next_msg["content"]
                                if not (isinstance(b, dict) and b.get("type") == "tool_result")
                            ]
                            if kept:
                                sanitized.append({"role": "user", "content": kept})
                            logger.warning("Stripped orphaned tool_result blocks from message at index %d", i + 1)
                            i += 2
                            continue
                i += 1
                continue

        # Check if a user message has tool_result blocks without a preceding tool_use
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            has_tool_results = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"])
            if has_tool_results:
                # Check if previous sanitized message is an assistant with matching tool_use
                prev = sanitized[-1] if sanitized else None
                prev_tool_ids: set[str] = set()
                if prev and prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                    prev_tool_ids = {
                        b["id"]
                        for b in prev["content"]
                        if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b
                    }
                result_ids = {
                    b.get("tool_use_id")
                    for b in msg["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                if not prev_tool_ids or not (result_ids <= prev_tool_ids):
                    # Orphaned tool_results — strip them, keep other content
                    kept = [b for b in msg["content"] if not (isinstance(b, dict) and b.get("type") == "tool_result")]
                    if kept:
                        sanitized.append({"role": "user", "content": kept})
                    logger.warning("Stripped orphaned tool_result blocks from user message at index %d", i)
                    i += 1
                    continue

        sanitized.append(msg)
        i += 1

    # Ensure history starts with a user message
    while sanitized and sanitized[0].get("role") != "user":
        sanitized.pop(0)

    # Removing orphans can create new orphans (e.g. a tool_result whose tool_use
    # was inside a dropped pair). Re-run until stable.
    if len(sanitized) < len(history):
        return _sanitize_history(sanitized)

    return sanitized


def get_conversation(chat_id: int) -> list:
    """Get conversation from cache or load from DB (sanitized)."""
    if chat_id not in conversations:
        loaded = load_conversation(chat_id)
        conversations[chat_id] = _sanitize_history(loaded)
    return conversations[chat_id]


def trim_history(chat_id: int) -> None:
    history = get_conversation(chat_id)
    if len(history) > MAX_HISTORY * 2:
        del history[: len(history) - MAX_HISTORY * 2]
    # Sanitize to fix any broken tool_use/tool_result pairs
    # IMPORTANT: modify in-place to preserve list reference held by _process_message
    sanitized = _sanitize_history(history)
    history.clear()
    history.extend(sanitized)
    cutoff = max(0, len(history) - _KEEP_IMAGES_LAST_N)
    for i, msg in enumerate(history):
        msg["content"] = _trim_content(msg.get("content"), keep_images=(i >= cutoff))


def save_state(chat_id: int) -> None:
    """Persist current conversation to SQLite."""
    save_conversation(chat_id, get_conversation(chat_id))
