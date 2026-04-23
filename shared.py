"""Shared utilities used by both bot.py and bot_agent.py."""

import collections
import functools
import logging
import re
import time
from html import escape

from telegram.error import TelegramError

logger = logging.getLogger(__name__)


# ── Logging ──────────────────────────────────────────────────────────


class RingBufferHandler(logging.Handler):
    """Keeps the last N log records in a deque for on-demand retrieval."""

    def __init__(self, capacity: int = 5000):
        super().__init__()
        self._buf: collections.deque[logging.LogRecord] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self._buf.append(record)

    def get_recent(self, seconds: int = 300) -> list[str]:
        """Return formatted log lines from the last `seconds` seconds."""
        cutoff = time.time() - seconds
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        return [formatter.format(r) for r in self._buf if r.created >= cutoff]


# ── Auth ─────────────────────────────────────────────────────────────


def is_authorized(user_id: int, allowed_ids: set[int]) -> bool:
    """Check if a user ID is in the allowlist. Empty allowlist permits all."""
    if not allowed_ids:
        return True
    return user_id in allowed_ids


def require_auth(allowed_ids: set[int]):
    """Decorator factory that checks authorization before running a handler.

    Usage:
        @require_auth(ALLOWED_USER_IDS)
        async def my_handler(update, context): ...
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update, context):
            if not is_authorized(update.effective_user.id, allowed_ids):
                try:
                    await update.message.reply_text("Sorry, you're not authorized to use this bot.")
                except TelegramError:
                    pass
                return
            return await func(update, context)

        return wrapper

    return decorator


# ── Markdown → Telegram HTML ─────────────────────────────────────────


def md_to_telegram_html(text: str) -> str:
    """Convert Markdown to Telegram-compatible HTML.

    Telegram supports: <b>, <i>, <s>, <u>, <code>, <pre>, <a href>.
    Everything else is converted to the nearest equivalent or stripped.
    """
    from markdown_it import MarkdownIt

    tokens = MarkdownIt().enable("table").enable("strikethrough").parse(text)
    result = _md_render_block(tokens)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _md_render_block(tokens: list) -> str:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        t = tok.type
        if t == "inline":
            out.append(_md_render_inline(tok.children or []))
        elif t == "paragraph_close":
            out.append("\n")
        elif t == "heading_open":
            i += 1
            content = _md_render_inline(tokens[i].children or [])
            i += 1  # heading_close
            out.append(f"<b>{content}</b>\n")
        elif t == "fence":
            code = escape(tok.content.rstrip("\n"))
            out.append(f"<pre><code>{code}</code></pre>\n")
        elif t == "code_block":
            out.append(f"<pre><code>{escape(tok.content.strip())}</code></pre>\n")
        elif t == "list_item_open":
            markup = (tok.info or "").strip()
            prefix = f"{markup} " if markup else "• "
            out.append(f"\n{prefix}")
        elif t in ("bullet_list_close", "ordered_list_close"):
            out.append("\n")
        elif t == "hr":
            out.append("\n──────────\n")
        elif t == "table_open":
            tbl: list = []
            i += 1
            depth = 1
            while i < len(tokens) and depth > 0:
                if tokens[i].type == "table_open":
                    depth += 1
                elif tokens[i].type == "table_close":
                    depth -= 1
                    if depth == 0:
                        break
                tbl.append(tokens[i])
                i += 1
            out.append(_md_render_table(tbl))
        i += 1
    return "".join(out)


def _md_render_inline(tokens: list) -> str:
    out: list[str] = []
    for tok in tokens:
        t = tok.type
        if t == "text":
            out.append(escape(tok.content))
        elif t in ("softbreak", "hardbreak"):
            out.append("\n")
        elif t == "code_inline":
            out.append(f"<code>{escape(tok.content)}</code>")
        elif t == "strong_open":
            out.append("<b>")
        elif t == "strong_close":
            out.append("</b>")
        elif t == "em_open":
            out.append("<i>")
        elif t == "em_close":
            out.append("</i>")
        elif t == "s_open":
            out.append("<s>")
        elif t == "s_close":
            out.append("</s>")
        elif t == "link_open":
            href = dict(tok.attrs or {}).get("href", "")
            out.append(f'<a href="{escape(href)}">')
        elif t == "link_close":
            out.append("</a>")
        elif t == "image":
            alt = "".join(escape(c.content) for c in (tok.children or []) if c.type == "text")
            out.append(alt)
        elif t == "html_inline":
            pass  # skip raw HTML
    return "".join(out)


def _md_render_table(tokens: list) -> str:
    """Render table tokens as an aligned-column <pre> block."""
    rows: list[list[str]] = []
    separator_after: set[int] = set()
    current_row: list[str] | None = None
    in_head = False

    for tok in tokens:
        t = tok.type
        if t == "thead_open":
            in_head = True
        elif t == "thead_close":
            in_head = False
        elif t == "tr_open":
            current_row = []
        elif t == "tr_close":
            if current_row is not None:
                rows.append(current_row)
                if in_head:
                    separator_after.add(len(rows) - 1)
                current_row = None
        elif t == "inline" and current_row is not None:
            cell = re.sub(r"<[^>]+>", "", _md_render_inline(tok.children or []))
            current_row.append(cell)

    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    widths = [0] * col_count
    for row in rows:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(cell))

    lines: list[str] = []
    for idx, row in enumerate(rows):
        cells = [row[j].ljust(widths[j]) if j < len(row) else " " * widths[j] for j in range(col_count)]
        lines.append("   ".join(cells).rstrip())
        if idx in separator_after:
            lines.append("─" * (sum(widths) + 3 * max(col_count - 1, 0)))

    return f"<pre>{escape(chr(10).join(lines))}</pre>\n"


# ── Telegram helpers ─────────────────────────────────────────────────

MAX_TELEGRAM_LENGTH = 4096


async def send_long_message(chat_id: int, text: str, bot, *, parse_mode: str | None = None) -> None:
    """Send a message, splitting at line boundaries to respect Telegram's 4096-char limit.

    If parse_mode='HTML', text is converted from Markdown to Telegram HTML first.
    """
    if not text:
        return
    if parse_mode == "HTML":
        text = md_to_telegram_html(text)
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > MAX_TELEGRAM_LENGTH:
            if current_parts:
                chunks.append("".join(current_parts))
                current_parts = []
                current_len = 0
            # Single line longer than the limit — hard split it
            while len(line) > MAX_TELEGRAM_LENGTH:
                chunks.append(line[:MAX_TELEGRAM_LENGTH])
                line = line[MAX_TELEGRAM_LENGTH:]
        current_parts.append(line)
        current_len += len(line)
    if current_parts:
        chunks.append("".join(current_parts))
    for chunk in chunks:
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        except TelegramError as e:
            logger.warning("Failed to send message chunk to %d: %s", chat_id, e)


async def download_telegram_file(file_obj, bot) -> bytes:
    """Download a Telegram file and return its bytes."""
    tg_file = await bot.get_file(file_obj.file_id)
    return bytes(await tg_file.download_as_bytearray())
