"""Teleclaude Agent — every message pipes straight to Claude Code CLI."""

from pathlib import Path

VERSION = (Path(__file__).parent / "VERSION").read_text().strip()

import asyncio
import datetime
import io
import logging
import os
import re
import shutil
import sys
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from claude_code import ClaudeCodeManager, get_claude_cli_version, update_claude_cli
from persistence import (
    audit_log,
    init_db,
    load_active_branch,
    load_active_repo,
    load_model,
    load_session_id,
    save_active_branch,
    save_active_repo,
    save_model,
    save_session_id,
)
from shared import (
    RingBufferHandler,
    download_telegram_file,
    send_long_message,
)
from shared import (
    is_authorized as _is_authorized,
)

load_dotenv()

_ring_handler = RingBufferHandler()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger().addHandler(_ring_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "opus")
CLAUDE_SESSION_KEY = os.getenv("CLAUDE_SESSION_KEY", "")
CLAUDE_ORG_ID = os.getenv("CLAUDE_ORG_ID", "")
CREDENTIALS_SYNC_TOKEN = os.getenv("CREDENTIALS_SYNC_TOKEN", "")
CREDENTIALS_PORT = int(os.getenv("CREDENTIALS_PORT", "0"))

# Use Claude Code CLI aliases — the CLI resolves these to the latest version
# per family, so new model releases don't require a bot redeploy.
AVAILABLE_MODELS = {
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
}


def _check_required_config() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        sys.exit(1)
    if not cli_path:
        logger.error("Claude CLI not found in PATH. Agent bot cannot function.")
        sys.exit(1)


if not GITHUB_TOKEN:
    logger.warning("GITHUB_TOKEN is not set — Claude Code will have no GitHub access.")

ALLOWED_USER_IDS: set[int] = set()
for uid in os.getenv("ALLOWED_USER_IDS", "").split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USER_IDS.add(int(uid))

# GitHub client (for /repo listing only)
gh_client = None
try:
    from github_tools import GitHubClient

    if GITHUB_TOKEN:
        gh_client = GitHubClient(GITHUB_TOKEN)
        logger.info("GitHub client: enabled (for /repo listing)")
except Exception as e:
    logger.warning("GitHub client: failed to load (%s)", e)

USER_TIMEZONE = os.getenv("TIMEZONE", "UTC")
MAX_TELEGRAM_LENGTH = 4096
TYPING_INTERVAL = 4

# ── Claude Code CLI ───────────────────────────────────────────────────

claude_code_mgr = None
cli_path = os.getenv("CLAUDE_CLI_PATH", "") or shutil.which("claude")
if cli_path:
    claude_code_mgr = ClaudeCodeManager(GITHUB_TOKEN, cli_path=cli_path)
    if GITHUB_TOKEN:
        logger.info("Claude Code CLI: enabled (path=%s)", claude_code_mgr.cli_path)
    else:
        logger.info("Claude Code CLI: enabled without GITHUB_TOKEN")

# ── State ─────────────────────────────────────────────────────────────

active_repos: dict[int, str] = {}
active_branches: dict[int, str] = {}
chat_models: dict[int, str] = {}
_plan_mode: set[int] = set()  # chat IDs with plan mode enabled
_stream_mode: set[int] = set()  # chat IDs with /newstream continuous mode
_typing_tasks: dict[int, asyncio.Task] = {}
_frag_buffers: dict[int, str] = {}  # chat_id -> buffered text from split pastes
_frag_tasks: dict[int, asyncio.Task] = {}  # chat_id -> pending flush task

_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
SKIP_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", "build", ".cache", ".tox"}
)
MAX_FILE_BYTES = 50 * 1024 * 1024  # Telegram bot file size limit
SEND_MARKER_RE = re.compile(r"\[SEND:\s*([^\]]+)\]", re.IGNORECASE)
ASK_MARKER_RE = re.compile(r"\[ASK:\s*([^\]]+)\]", re.IGNORECASE)

_files_cache: dict[int, list[Path]] = {}  # chat_id -> file list for inline keyboard
_ask_options: dict[int, list[str]] = {}  # chat_id -> options for current [ASK:] question


def is_authorized(user_id: int) -> bool:
    return _is_authorized(user_id, ALLOWED_USER_IDS)


def get_model(chat_id: int) -> str:
    if chat_id not in chat_models:
        saved = load_model(chat_id)
        if saved:
            chat_models[chat_id] = saved
    return chat_models.get(chat_id, DEFAULT_MODEL)


def get_active_repo(chat_id: int) -> str | None:
    if chat_id not in active_repos:
        repo = load_active_repo(chat_id)
        if repo:
            active_repos[chat_id] = repo
    return active_repos.get(chat_id)


def get_active_branch(chat_id: int) -> str | None:
    if chat_id not in active_branches:
        branch = load_active_branch(chat_id)
        if branch:
            active_branches[chat_id] = branch
    return active_branches.get(chat_id)


def set_active_branch(chat_id: int, branch: str | None) -> None:
    if branch:
        active_branches[chat_id] = branch
    elif chat_id in active_branches:
        del active_branches[chat_id]
    save_active_branch(chat_id, branch)


# ── Helpers ───────────────────────────────────────────────────────────


def _format_tool_progress(block: dict) -> str | None:
    """Format a progress block into a readable line.

    Handles tool_use blocks, text (reasoning) blocks, and synthetic _type events (stats, system_event).
    """
    synthetic = block.get("_type")

    if synthetic == "stats":
        parts = []
        cost = block.get("cost_usd")
        if cost is not None:
            parts.append(f"${cost:.4f}")
        turns = block.get("num_turns")
        if turns is not None:
            parts.append(f"{turns} turns")
        usage = block.get("usage") or {}
        total_tok = (
            usage.get("input_tokens", 0)
            + usage.get("output_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        if total_tok:
            parts.append(f"{total_tok:,} tok")
        return " · ".join(parts) if parts else None

    if synthetic == "system_event":
        subtype = block.get("subtype", "").lower()
        if "compact" in subtype:
            return "Context compacted"
        return None

    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text", "").strip()
        if not text:
            return None
        return text

    # Tool use blocks

    name = block.get("name", "")
    inp = block.get("input", {})

    if name == "Read":
        path = inp.get("file_path", "")
        return f"Reading {_short_path(path)}" if path else None
    if name == "Write":
        path = inp.get("file_path", "")
        return f"Writing {_short_path(path)}" if path else None
    if name == "Edit":
        path = inp.get("file_path", "")
        return f"Editing {_short_path(path)}" if path else None
    if name == "Bash":
        cmd = inp.get("command", "")
        if not cmd:
            return None
        first_line = cmd.split("\n", 1)[0]
        suffix = "…" if "\n" in cmd else ""
        return f"$ {first_line}{suffix}"
    if name == "Glob":
        pattern = inp.get("pattern", "")
        return f"Finding {pattern}" if pattern else None
    if name == "Grep":
        pattern = inp.get("pattern", "")
        return f"Searching: {pattern}" if pattern else None
    if name == "Task":
        desc = inp.get("description", "")
        return f"Subagent: {desc}" if desc else None
    # Generic fallback
    return name.replace("_", " ").title() if name else None


def _short_path(path: str) -> str:
    """Shorten a file path to last 2-3 components."""
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else path


def _start_stream_typing(chat_id: int, bot) -> None:
    """Start a background typing indicator for stream mode turns."""
    task = _typing_tasks.get(chat_id)
    if task and not task.done():
        return

    async def _loop() -> None:
        try:
            while True:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(TYPING_INTERVAL)
        except (asyncio.CancelledError, TelegramError, Exception):
            pass

    _typing_tasks[chat_id] = asyncio.create_task(_loop())


def _stop_stream_typing(chat_id: int) -> None:
    """Cancel the stream-mode typing indicator for this chat."""
    task = _typing_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def _save_attachment(chat_id: int, data: bytes, mime: str, label: str = "") -> str:
    """Save attachment to shared dir, return absolute path."""
    shared_dir = claude_code_mgr.workspace_root / ".shared" / str(chat_id)
    shared_dir.mkdir(parents=True, exist_ok=True)
    ext = _MIME_TO_EXT.get(mime, "")
    # Sanitize label to prevent path traversal
    safe_label = label.replace("/", "_").replace("\\", "_").replace("..", "_")
    name = f"{safe_label}_{int(time.time())}{ext}" if safe_label else f"{int(time.time())}{ext}"
    path = (shared_dir / name).resolve()
    # Verify the resolved path is still inside the shared dir
    if not str(path).startswith(str(shared_dir.resolve())):
        raise ValueError(f"Path traversal blocked in attachment save: {name!r}")
    path.write_bytes(data)
    return str(path)


async def _download_telegram_file(file_obj, bot) -> bytes:
    return await download_telegram_file(file_obj, bot)


def _list_workspace_files(workspace: Path, limit: int = 5) -> list[Path]:
    files = []
    try:
        for f in workspace.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(workspace)
            if any(p.startswith(".") or p in SKIP_DIRS for p in rel.parts[:-1]):
                continue
            if rel.parts[-1].startswith("."):
                continue
            files.append(f)
    except PermissionError:
        pass
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:limit]


async def _send_file_to_user(chat_id: int, path: Path, bot) -> bool:
    if not path.exists():
        await bot.send_message(chat_id=chat_id, text=f"File not found: {path.name}")
        return False
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        await bot.send_message(chat_id=chat_id, text=f"{path.name} is too large to send ({size // 1024 // 1024}MB).")
        return False
    try:
        with open(path, "rb") as fh:
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                await bot.send_photo(chat_id=chat_id, photo=fh, caption=path.name)
            else:
                await bot.send_document(chat_id=chat_id, document=fh, filename=path.name)
        return True
    except Exception as e:
        logger.error("Failed to send file %s: %s", path, e)
        try:
            await bot.send_message(chat_id=chat_id, text=f"Failed to send {path.name}: {e}")
        except TelegramError:
            pass
        return False


async def _parse_and_send_markers(chat_id: int, text: str, repo: str | None, bot) -> str:
    """Strip [SEND: path] and [ASK: question | opt1 | opt2] markers from text and handle them."""
    send_markers = SEND_MARKER_RE.findall(text)
    workspace = claude_code_mgr.workspace_path(repo) if repo else None
    workspace_str = str(workspace.resolve()) if workspace else None
    shared_str = str((claude_code_mgr.workspace_root / ".shared" / str(chat_id)).resolve())
    tmp_str = str(Path("/tmp").resolve())
    for raw in send_markers:
        raw = raw.strip()
        p = Path(raw)
        if not p.is_absolute() and workspace:
            p = (workspace / raw).resolve()
        else:
            p = p.resolve()
        safe = (
            (workspace_str and str(p).startswith(workspace_str))
            or str(p).startswith(shared_str)
            or str(p).startswith(tmp_str)
        )
        if safe:
            await _send_file_to_user(chat_id, p, bot)
        else:
            logger.warning("Blocked file send outside workspace: %s", p)
    text = SEND_MARKER_RE.sub("", text).strip()

    for match in ASK_MARKER_RE.finditer(text):
        parts = [p.strip() for p in match.group(1).split("|")]
        if len(parts) >= 3:
            question, options = parts[0], parts[1:]
            _ask_options[chat_id] = options
            buttons = [
                [InlineKeyboardButton(opt, callback_data=f"ask_agent:{chat_id}:{i}")] for i, opt in enumerate(options)
            ]
            await bot.send_message(chat_id=chat_id, text=question, reply_markup=InlineKeyboardMarkup(buttons))
    text = ASK_MARKER_RE.sub("", text).strip()

    return text


# ── Commands ──────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    repo = get_active_repo(update.effective_chat.id)
    repo_line = f"\nActive repo: {repo}" if repo else ""

    await update.message.reply_text(
        f"Teleclaude Agent — Claude Code on Telegram.{repo_line}\n\n"
        "Every message goes straight to Claude Code CLI via continuous stream mode.\n\n"
        "Commands:\n"
        "/repo owner/name - Set the active GitHub repo\n"
        "/repo - Show current repo\n"
        "/branch name - Set active branch\n"
        "/files - Browse and download workspace files\n"
        "/plan - Toggle plan mode (read-only)\n"
        "/plan <task> - Plan a specific task\n"
        "/work - Exit plan mode\n"
        "/btw <question> - Ask a side question without interrupting\n"
        "- <message> - Send extra info while Claude is working\n"
        "/cancel - Soft interrupt (Esc equivalent — keeps process + session)\n"
        "/stop - Stop current work (kills CC process, keeps session)\n"
        "/newstream - Wipe this repo's session and restart fresh (updates CLI)\n"
        "/restart - Restart CC and resume this repo's last session (updates CLI)\n"
        "/update - Update Claude CLI to latest version\n"
        "/model - Show or switch model (opus/sonnet/haiku)\n"
        "/logs [min] - Download recent logs\n"
        "/version - Show bot version\n"
        "/help - Show this message"
    )


def _find_repo_candidates(name: str, limit: int = 5) -> list[str]:
    """Resolve a bare repo name to up to `limit` 'owner/name' candidates.

    Looks first at locally cloned repos under workspaces/pzfreo/, then at the
    GitHub user's most-recently-pushed repos. Case-insensitive substring match.
    Local matches are preferred and listed first.
    """
    needle = name.lower()
    seen: set[str] = set()
    candidates: list[str] = []

    if claude_code_mgr:
        local_root = claude_code_mgr.workspace_root / "pzfreo"
        try:
            local_dirs = sorted(p.name for p in local_root.iterdir() if p.is_dir())
        except (FileNotFoundError, NotADirectoryError):
            local_dirs = []
        for d in local_dirs:
            if needle in d.lower():
                full = f"pzfreo/{d}"
                if full not in seen:
                    seen.add(full)
                    candidates.append(full)
                    if len(candidates) >= limit:
                        return candidates

    if gh_client:
        try:
            repos = gh_client.list_user_repos(100)
        except Exception as e:
            logger.warning("list_user_repos failed during search: %s", e)
            repos = []
        for r in repos:
            full = r["full_name"]
            if needle in full.lower() and full not in seen:
                seen.add(full)
                candidates.append(full)
                if len(candidates) >= limit:
                    break

    return candidates


async def set_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        repo = get_active_repo(chat_id)
        header_lines = []
        if repo:
            branch = get_active_branch(chat_id)
            header_lines.append("Active repo: " + repo + (f" ({branch})" if branch else ""))

        if gh_client:
            try:
                loop = asyncio.get_running_loop()
                repos = await loop.run_in_executor(None, gh_client.list_user_repos, 5)
                buttons = []
                for r in repos:
                    label = r["full_name"] + (" ✓" if r["full_name"] == repo else "")
                    buttons.append([InlineKeyboardButton(label, callback_data=f"repo:{r['full_name']}")])
                markup = InlineKeyboardMarkup(buttons)
                header = "\n".join(header_lines) + ("\n\n" if header_lines else "") + "Tap a repo to switch:"
                await update.message.reply_text(header, reply_markup=markup)
            except Exception as e:
                logger.warning("Failed to list repos: %s", e)
                msg = "\n".join(header_lines) or "No repo set. Use: /repo owner/name"
                await update.message.reply_text(msg)
        else:
            msg = "\n".join(header_lines) or "No repo set. Use: /repo owner/name"
            await update.message.reply_text(msg)
        return

    arg = context.args[0]

    # Pick from recent list by number
    if arg.isdigit() and gh_client:
        try:
            loop = asyncio.get_running_loop()
            repos = await loop.run_in_executor(None, gh_client.list_user_repos, 5)
            idx = int(arg) - 1
            if 0 <= idx < len(repos):
                repo = repos[idx]["full_name"]
            else:
                await update.message.reply_text(f"Invalid number. Use 1-{len(repos)}.")
                return
        except Exception as e:
            await update.message.reply_text(f"Failed to list repos: {e}")
            return
    else:
        repo = arg
        if "/" not in repo or len(repo.split("/")) != 2:
            loop = asyncio.get_running_loop()
            matches = await loop.run_in_executor(None, _find_repo_candidates, arg)
            if not matches:
                await update.message.reply_text(f"No repo found matching '{arg}'. Use: /repo owner/name")
                return
            if len(matches) == 1:
                repo = matches[0]
                await update.message.reply_text(f"Matched: {repo}")
            else:
                buttons = [[InlineKeyboardButton(m, callback_data=f"repo:{m}")] for m in matches]
                await update.message.reply_text(
                    f"Multiple matches for '{arg}'. Tap one:",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                return

    # If stream mode is active, tear it down before switching repos — the
    # running CC process has cwd pointing at the old repo. We'll relaunch
    # against the new repo after the clone finishes. Sessions are per-(chat,
    # repo), so the old repo's session is left in place untouched and will
    # resume next time you switch back.
    was_streaming = chat_id in _stream_mode
    if was_streaming:
        _stream_mode.discard(chat_id)
        await claude_code_mgr.stop_stream(chat_id, kill_proc=True)

    active_repos[chat_id] = repo
    save_active_repo(chat_id, repo)
    set_active_branch(chat_id, None)
    msg = f"Active repo set to: {repo}\nCloning workspace..."
    await update.message.reply_text(msg)

    async def _clone_notify():
        try:
            await claude_code_mgr.ensure_clone(repo)
            await update.message.reply_text(f"Workspace ready: {repo}")
            usage = await _fetch_usage_text()
            if usage:
                await update.message.reply_text(usage)
        except Exception as e:
            logger.error("Clone failed: %s", e)
            await update.message.reply_text(f"Clone failed: {e}")
            return

        if was_streaming:
            on_event = _make_stream_event_handler(chat_id, context.bot)
            try:
                await claude_code_mgr.start_stream(
                    chat_id=chat_id,
                    repo=repo,
                    on_event=on_event,
                    branch=None,
                    model=get_model(chat_id),
                    permission_mode="plan" if chat_id in _plan_mode else None,
                )
            except Exception as e:
                logger.error("Failed to restart stream on new repo: %s", e, exc_info=True)
                await update.message.reply_text(f"Stream restart failed: {e}")
                return
            _stream_mode.add(chat_id)
            await update.message.reply_text(f"Stream mode restarted on `{repo}`.", parse_mode="Markdown")

    asyncio.create_task(_clone_notify())


async def repo_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /1 through /5 as shortcuts for /repo 1 through /repo 5."""
    context.args = [update.effective_message.text.lstrip("/").split()[0]]
    await set_repo(update, context)


async def set_branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    repo = get_active_repo(chat_id)

    if not context.args:
        branch = get_active_branch(chat_id)
        if branch:
            await update.message.reply_text(f"Active branch: {branch}\n/branch clear to reset")
        else:
            await update.message.reply_text("No branch set (using default). /branch <name> to set one.")
        return

    arg = context.args[0]
    if arg.lower() == "clear":
        set_active_branch(chat_id, None)
        await update.message.reply_text("Branch cleared.")
        return

    branch_name = arg
    set_active_branch(chat_id, branch_name)
    msg = f"Active branch set to: {branch_name}"

    if repo:
        ws = claude_code_mgr.workspace_path(repo)
        if (ws / ".git").is_dir():
            try:
                await claude_code_mgr.checkout_branch(repo, branch_name)
                msg += " (checked out locally)"
            except Exception as e:
                msg += f" (local checkout failed: {e})"

    await update.message.reply_text(msg)


async def cancel_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Softly interrupt the current turn — keeps CC process and session alive.

    Equivalent to pressing Esc in interactive Claude Code. Use /stop to kill
    the process entirely.
    """
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    if not claude_code_mgr.has_running_proc(chat_id):
        await update.message.reply_text("Nothing running.")
        return

    sent = await claude_code_mgr.interrupt(chat_id)
    if sent:
        _stop_stream_typing(chat_id)
        await update.message.reply_text("Interrupt sent. Session preserved — send a new message to continue.")
    else:
        await update.message.reply_text(
            "Couldn't send interrupt (process may have already exited). Use /stop if it's stuck."
        )


async def stop_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    was_streaming = chat_id in _stream_mode
    _stream_mode.discard(chat_id)
    _stop_stream_typing(chat_id)
    was_running = await claude_code_mgr.abort(chat_id)
    if was_streaming:
        await update.message.reply_text("Stream stopped.")
    elif was_running:
        await update.message.reply_text("Stopped.")
    else:
        await update.message.reply_text("Nothing running.")


async def update_cli(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update Claude CLI to latest version."""
    if not is_authorized(update.effective_user.id):
        return

    await update.message.reply_text("Checking for Claude CLI updates...")

    success, msg = await update_claude_cli()
    if success:
        await update.message.reply_text(f"✅ Claude CLI {msg}")
    else:
        # Show current version even if update failed
        version = await get_claude_cli_version()
        status = f"Current version: {version}\n" if version else ""
        await update.message.reply_text(f"{status}Info: {msg}")


async def _report_cli_update_status(update: Update) -> None:
    """Run npm update for the Claude CLI and post a one-line status to the chat."""
    success, msg = await update_claude_cli()
    if success:
        await update.message.reply_text(f"✅ Claude CLI {msg}")
    elif "permission" not in msg.lower():
        await update.message.reply_text(f"Info: {msg}")
    else:
        # Permission error usually just means we're already on the latest version.
        version = await get_claude_cli_version()
        if version:
            await update.message.reply_text(f"Claude CLI version: {version}")


def _make_stream_event_handler(chat_id: int, bot):
    """Build an on_event callback that renders CC stream-json events into Telegram."""

    async def on_event(event: dict) -> None:
        event_type = event.get("type")

        if event_type == "assistant":
            message = event.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype in ("text", "tool_use"):
                        line = _format_tool_progress(block)
                        if line:
                            try:
                                if btype == "text":
                                    line = await _parse_and_send_markers(chat_id, line, active_repos.get(chat_id), bot)
                                if line:
                                    pm = "HTML" if btype == "text" else None
                                    await send_long_message(chat_id, line, bot, parse_mode=pm)
                            except TelegramError:
                                pass
            return

        if event_type == "result":
            _stop_stream_typing(chat_id)
            stats = {k: event[k] for k in ("cost_usd", "num_turns", "usage") if k in event}
            if stats:
                line = _format_tool_progress({"_type": "stats", **stats})
                if line:
                    try:
                        await send_long_message(chat_id, line, bot)
                    except TelegramError:
                        pass
            return

        if event_type == "system":
            subtype = event.get("subtype")
            if subtype and subtype != "init":
                line = _format_tool_progress({"_type": "system_event", "subtype": subtype})
                if line:
                    try:
                        await send_long_message(chat_id, line, bot)
                    except TelegramError:
                        pass
            return

        # Synthetic stream-end signal emitted by ClaudeCodeManager._stream_forever
        if event.get("_type") == "stream_end":
            _stop_stream_typing(chat_id)
            _stream_mode.discard(chat_id)
            try:
                await send_long_message(chat_id, "Stream ended — send a message to continue.", bot)
            except TelegramError:
                pass

    return on_event


async def new_stream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipe this repo's session, update Claude CLI, and relaunch a fresh stream."""
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return

    # Tear down any existing stream / process for this chat.
    _stream_mode.discard(chat_id)
    await claude_code_mgr.stop_stream(chat_id, kill_proc=True)
    await claude_code_mgr.abort(chat_id)

    # Clear ONLY this repo's session. Other repos in this chat keep their memory.
    claude_code_mgr.new_session(chat_id, repo)
    save_session_id(chat_id, repo, None)

    await update.message.reply_text("Updating Claude CLI…")
    await _report_cli_update_status(update)

    branch = get_active_branch(chat_id)
    err = await _start_stream_for_chat(chat_id, repo, context.bot)
    if err:
        await update.message.reply_text(err)
        return

    label = f"`{repo}`" + (f" on `{branch}`" if branch else "")
    await update.message.reply_text(
        f"Stream mode ON — fresh session — {label}\n"
        "Messages feed into CC continuously. Scheduled events post here as they fire.",
        parse_mode="Markdown",
    )
    usage = await _fetch_usage_text()
    if usage:
        await update.message.reply_text(usage)


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill CC, update the CLI, and relaunch the stream resuming this repo's session."""
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return

    # Pull the session ID from memory or DB so the relaunched proc resumes it.
    session_id = claude_code_mgr.get_session_id(chat_id, repo)
    if session_id is None:
        saved = load_session_id(chat_id, repo)
        if saved:
            claude_code_mgr._sessions[(chat_id, repo)] = saved
            session_id = saved

    # Tear down stream + proc, but DON'T clear the session — we want to resume.
    _stream_mode.discard(chat_id)
    await claude_code_mgr.stop_stream(chat_id, kill_proc=True)
    await claude_code_mgr.abort(chat_id)

    await update.message.reply_text("Updating Claude CLI…")
    await _report_cli_update_status(update)

    err = await _start_stream_for_chat(chat_id, repo, context.bot)
    if err:
        await update.message.reply_text(err)
        return

    branch = get_active_branch(chat_id)
    label = f"`{repo}`" + (f" on `{branch}`" if branch else "")
    if session_id:
        await update.message.reply_text(
            f"Resumed session `{session_id[:8]}…` on {label}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"No prior session for {label} — started fresh.",
            parse_mode="Markdown",
        )
    usage = await _fetch_usage_text()
    if usage:
        await update.message.reply_text(usage)


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    if not context.args:
        model = get_model(chat_id)
        resolved = claude_code_mgr.get_last_model(chat_id)
        if resolved is None:
            resolved = await claude_code_mgr.probe_resolved_model(model)
        if resolved and resolved != model:
            status = f"Current: {model} (resolved: {resolved})"
        elif resolved:
            status = f"Current: {resolved}"
        else:
            status = f"Current: {model}"
        buttons = [
            [InlineKeyboardButton(name + (" ✓" if name == model else ""), callback_data=f"model:{name}")]
            for name in AVAILABLE_MODELS
        ]
        markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(f"{status}\n\nTap to switch:", reply_markup=markup)
        return

    choice = context.args[0].lower().strip()
    if choice in AVAILABLE_MODELS:
        model_id = AVAILABLE_MODELS[choice]
    elif choice.startswith("claude-"):
        model_id = choice
    else:
        await update.message.reply_text(f"Unknown model: {choice}")
        return

    chat_models[chat_id] = model_id
    save_model(chat_id, model_id)
    claude_code_mgr.clear_last_model(chat_id)
    await update.message.reply_text(f"Model switched to: {model_id}")


async def send_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    minutes = 5
    if context.args:
        try:
            minutes = max(1, min(int(context.args[0]), 60))
        except ValueError:
            pass
    lines = _ring_handler.get_recent(seconds=minutes * 60)
    if not lines:
        await update.message.reply_text(f"No logs in the last {minutes} minute(s).")
        return
    content = "\n".join(lines)
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"agent_logs_{minutes}min.txt"
    await update.message.reply_document(document=buf, caption=f"Last {minutes} min — {len(lines)} lines")


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return
    workspace = claude_code_mgr.workspace_path(repo)
    if not workspace.exists():
        await update.message.reply_text("Workspace not cloned yet. Set a repo first.")
        return
    files = _list_workspace_files(workspace)
    if not files:
        await update.message.reply_text("No files found in workspace.")
        return
    _files_cache[chat_id] = files
    buttons = []
    for i, f in enumerate(files):
        rel = f.relative_to(workspace)
        size = f.stat().st_size
        size_str = f"{size / 1024:.1f}KB" if size >= 1024 else f"{size}B"
        buttons.append([InlineKeyboardButton(f"{rel}  ({size_str})", callback_data=f"dl:{i}")])
    markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Recent files — tap to download:", reply_markup=markup)


async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline keyboard callbacks: dl:, repo:, model:"""
    query = update.callback_query
    if not query or not query.from_user:
        return
    if not is_authorized(query.from_user.id):
        await query.answer("Not authorized.")
        return
    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat_id

    if data.startswith("dl:"):
        try:
            idx = int(data[3:])
        except ValueError:
            return
        files = _files_cache.get(chat_id, [])
        if idx >= len(files):
            await query.edit_message_text("File list expired. Use /files again.")
            return
        await _send_file_to_user(chat_id, files[idx], context.bot)

    elif data.startswith("repo:"):
        repo = data[5:]
        if "/" not in repo:
            return
        was_streaming = chat_id in _stream_mode
        if was_streaming:
            _stream_mode.discard(chat_id)
            await claude_code_mgr.stop_stream(chat_id, kill_proc=True)
        active_repos[chat_id] = repo
        save_active_repo(chat_id, repo)
        set_active_branch(chat_id, None)
        # Sessions are per-(chat, repo); switching does NOT clear the new
        # repo's stored session, so memory resumes when stream auto-starts.
        await query.edit_message_text(f"Switching to {repo}…")

        async def _clone_and_reply():
            try:
                await claude_code_mgr.ensure_clone(repo)
                await context.bot.send_message(chat_id=chat_id, text=f"Workspace ready: {repo}")
            except Exception as e:
                logger.error("Clone failed: %s", e)
                await context.bot.send_message(chat_id=chat_id, text=f"Clone failed: {e}")
                return
            if was_streaming:
                on_event = _make_stream_event_handler(chat_id, context.bot)
                try:
                    await claude_code_mgr.start_stream(
                        chat_id=chat_id,
                        repo=repo,
                        on_event=on_event,
                        branch=None,
                        model=get_model(chat_id),
                        permission_mode="plan" if chat_id in _plan_mode else None,
                    )
                    _stream_mode.add(chat_id)
                    await context.bot.send_message(
                        chat_id=chat_id, text=f"Stream restarted on `{repo}`.", parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error("Stream restart failed: %s", e)
                    await context.bot.send_message(chat_id=chat_id, text=f"Stream restart failed: {e}")

        asyncio.create_task(_clone_and_reply())

    elif data.startswith("model:"):
        name = data[6:]
        if name not in AVAILABLE_MODELS:
            return
        model_id = AVAILABLE_MODELS[name]
        chat_models[chat_id] = model_id
        save_model(chat_id, model_id)
        claude_code_mgr.clear_last_model(chat_id)
        await query.edit_message_text(f"Model switched to: {model_id}")

    elif data.startswith("ask_agent:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        try:
            target_chat = int(parts[1])
            idx = int(parts[2])
        except ValueError:
            return
        options = _ask_options.get(target_chat, [])
        if idx >= len(options):
            await query.edit_message_text("Options expired — send your answer as a message.")
            return
        answer = options[idx]
        _ask_options.pop(target_chat, None)
        await query.edit_message_text(f"You chose: {answer}")
        sent = await claude_code_mgr.feed(target_chat, answer)
        if not sent:
            await context.bot.send_message(
                chat_id=target_chat,
                text=f"You chose: {answer}\n(No active session — your choice was not sent to Claude.)",
            )


def _get_usage_credentials() -> tuple[str, str]:
    """Return (session_key, org_id): file-synced credentials take priority over env vars."""
    from persistence import load_claude_credentials

    file_key, file_org = load_claude_credentials()
    return (file_key or CLAUDE_SESSION_KEY, file_org or CLAUDE_ORG_ID)


async def _fetch_usage_text() -> str:
    """Return a one-line usage summary, or empty string if unavailable."""
    session_key, org_id = _get_usage_credentials()
    if not session_key or not org_id:
        return ""
    import aiohttp

    url = f"https://claude.ai/api/organizations/{org_id}/usage"
    headers = {
        "accept": "*/*",
        "anthropic-client-platform": "web_claude_ai",
        "anthropic-client-version": "1.0.0",
        "content-type": "application/json",
        "cookie": f"sessionKey={session_key}",
    }
    try:
        async with aiohttp.ClientSession() as session, session.get(url, headers=headers) as resp:
            if not resp.ok:
                return ""
            data = await resp.json()
    except Exception:
        return ""

    labels = {"five_hour": "Sess", "seven_day": "Wk", "seven_day_sonnet": "Wk(S)"}
    parts = []
    for key, label in labels.items():
        if key in data and data[key] is not None:
            pct = round(data[key].get("utilization", 0))
            parts.append(f"{label}: {pct}%")
    return "Usage — " + " | ".join(parts) if parts else ""


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage — show Claude plan usage limits."""
    if not is_authorized(update.effective_user.id):
        return
    session_key, org_id = _get_usage_credentials()
    if not session_key or not org_id:
        msg = "Claude usage not configured."
        if CREDENTIALS_SYNC_TOKEN and CREDENTIALS_PORT:
            msg += " Use the browser extension Sync button to push credentials."
        else:
            msg += " Set CLAUDE_SESSION_KEY and CLAUDE_ORG_ID in .env.agent"
        await update.message.reply_text(msg)
        return
    import aiohttp

    url = f"https://claude.ai/api/organizations/{org_id}/usage"
    headers = {
        "accept": "*/*",
        "anthropic-client-platform": "web_claude_ai",
        "anthropic-client-version": "1.0.0",
        "content-type": "application/json",
        "cookie": f"sessionKey={session_key}",
    }
    try:
        async with aiohttp.ClientSession() as session, session.get(url, headers=headers) as resp:
            if resp.status == 401:
                await update.message.reply_text(
                    "Session expired. Use the browser extension Sync button to push fresh credentials."
                )
                return
            resp.raise_for_status()
            data = await resp.json()
    except Exception as e:
        logger.error("Failed to fetch Claude usage: %s", e)
        await update.message.reply_text(f"Error fetching usage: {e}")
        return

    lines = []
    labels = {
        "five_hour": "Session",
        "seven_day": "Weekly (all)",
        "seven_day_sonnet": "Weekly (Sonnet)",
    }
    for key, label in labels.items():
        if key in data and data[key] is not None:
            pct = round(data[key].get("utilization", 0))
            resets_at = data[key].get("resets_at", "")
            reset_str = f" — resets {resets_at[:10]}" if resets_at else ""
            lines.append(f"{label}: {pct}%{reset_str}")
    if lines:
        await update.message.reply_text("Claude usage:\n" + "\n".join(lines))
    else:
        await update.message.reply_text("No usage data returned.")


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(f"Teleclaude Agent v{VERSION}\nModel: {get_model(update.effective_chat.id)}")


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /plan — toggle plan mode or plan a specific task."""
    if not update.message or not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    # Strip "/plan" prefix
    task = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""

    if not task:
        # Toggle plan mode on
        _plan_mode.add(chat_id)
        await update.message.reply_text("Plan mode ON. Claude will plan but not implement.\nSend /work to switch back.")
        return

    # Send the framed planning prompt through the normal stream path
    prompt = (
        "Think carefully and create a plan for this task. Present the full plan in your response so I can review it. "
        "Do NOT start implementing until I approve.\n\n"
        f"Task: {task}"
    )
    await _dispatch_prompt(chat_id, prompt, update, context)


async def work_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /work — exit plan mode."""
    if not update.message or not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    was_planning = chat_id in _plan_mode
    _plan_mode.discard(chat_id)
    if was_planning:
        await update.message.reply_text("Plan mode OFF. Claude will now implement directly.")
    else:
        await update.message.reply_text("Already in work mode.")


async def btw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /btw — send a side question to the running process."""
    if not update.message or not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    question = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""

    if not question:
        await update.message.reply_text(
            "Usage: /btw <question>\nAsk a quick side question without interrupting the main task."
        )
        return

    framed = f"BTW (side question — answer briefly, don't change your current task): {question}"

    if claude_code_mgr.stream_mode_active(chat_id):
        sent = await claude_code_mgr.send_followup(chat_id, framed)
        if sent:
            await update.message.reply_text("Side question sent.")
            return

    # No active stream (or feed failed) — fall back to a normal message and notify.
    await update.message.reply_text("No active session — sending as a normal message.")
    await _dispatch_prompt(chat_id, question, update, context)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_authorized(update.effective_user.id):
        return
    cmd = update.message.text.split()[0] if update.message.text else "/?"
    await update.message.reply_text(f"Unknown command: {cmd}\nType /help to see available commands.")


# ── Message handling ──────────────────────────────────────────────────


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_authorized(update.effective_user.id):
        try:
            await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        except TelegramError:
            pass
        return

    chat_id = update.effective_chat.id
    msg = update.message
    text = msg.text or msg.caption or ""
    attachment_paths = []

    # Download and save attachments
    if msg.photo:
        try:
            data = await _download_telegram_file(msg.photo[-1], context.bot)
            path = _save_attachment(chat_id, data, "image/jpeg", "photo")
            attachment_paths.append(path)
        except Exception as e:
            logger.warning("Failed to download photo: %s", e)

    if msg.sticker and not msg.sticker.is_animated and not msg.sticker.is_video:
        try:
            data = await _download_telegram_file(msg.sticker, context.bot)
            path = _save_attachment(chat_id, data, "image/webp", "sticker")
            attachment_paths.append(path)
            if not text:
                text = f"[Sticker: {msg.sticker.emoji or 'unknown'}]"
        except Exception as e:
            logger.warning("Failed to download sticker: %s", e)

    if msg.document:
        mime = msg.document.mime_type or ""
        fname = msg.document.file_name or "file"
        try:
            data = await _download_telegram_file(msg.document, context.bot)
            ext = _MIME_TO_EXT.get(mime, "")
            if not ext:
                # Derive from filename
                ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else ""
            path = _save_attachment(chat_id, data, mime, fname.rsplit(".", 1)[0])
            attachment_paths.append(path)
        except Exception as e:
            logger.warning("Failed to download document: %s", e)
            text += f"\n[Attached file: {fname} — download failed]"

    if msg.voice:
        text += "\n[Voice message — not supported. Please type instead.]"

    if not text and not attachment_paths:
        return

    # // prefix: pass a Claude Code slash command directly to the CLI
    if text.startswith("//") and not attachment_paths:
        cli_cmd = text[1:]  # strip one slash → e.g. //usage becomes /usage
        sent = await claude_code_mgr.send_followup(chat_id, cli_cmd)
        if not sent:
            await update.message.reply_text(f"No active session. Start one with /newstream, then retry {text}")
        return

    # Build prompt with attachment paths
    prompt = text
    if attachment_paths:
        paths_str = "\n".join(f"  - {p}" for p in attachment_paths)
        prompt += f"\n\nAttached files (saved to disk, you can read them):\n{paths_str}"

    user_id = update.effective_user.id if update.effective_user else None
    text_preview = (text[:80] + "...") if len(text) > 80 else text
    audit_log("agent_message", chat_id=chat_id, user_id=user_id, detail=text_preview)

    # Telegram splits pastes longer than MAX_TELEGRAM_LENGTH into consecutive full-length
    # messages. Buffer text-only fragments and wait 1 s for a continuation before dispatching.
    if len(text) == MAX_TELEGRAM_LENGTH and not attachment_paths:
        _frag_buffers[chat_id] = _frag_buffers.get(chat_id, "") + text
        if chat_id not in _frag_tasks:

            async def _flush(cid=chat_id, u=update, ctx=context):
                await asyncio.sleep(1.0)
                buf = _frag_buffers.pop(cid, "")
                _frag_tasks.pop(cid, None)
                if buf:
                    await _dispatch_prompt(cid, buf, u, ctx)

            _frag_tasks[chat_id] = asyncio.create_task(_flush())
        return  # don't process this fragment yet

    # Final fragment or regular message — cancel any pending flush and prepend buffered text
    if chat_id in _frag_buffers:
        pending = _frag_tasks.pop(chat_id, None)
        if pending:
            pending.cancel()
        prompt = _frag_buffers.pop(chat_id) + prompt

    await _dispatch_prompt(chat_id, prompt, update, context)


async def _start_stream_for_chat(chat_id: int, repo: str, bot) -> str | None:
    """Start a stream for this chat. Returns None on success, or an error string."""
    on_event = _make_stream_event_handler(chat_id, bot)
    try:
        await claude_code_mgr.start_stream(
            chat_id=chat_id,
            repo=repo,
            on_event=on_event,
            branch=get_active_branch(chat_id),
            model=get_model(chat_id),
            permission_mode="plan" if chat_id in _plan_mode else None,
        )
    except Exception as e:
        logger.error("Stream start failed for chat %d: %s", chat_id, e, exc_info=True)
        _stream_mode.discard(chat_id)
        return f"Failed to start Claude Code: {e}"
    _stream_mode.add(chat_id)
    return None


async def _dispatch_prompt(chat_id: int, prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route an assembled prompt to the running CC stream, starting one if needed."""
    # "-" prefix: send as follow-up to running process
    is_followup = prompt.startswith("-") and len(prompt) > 1
    if is_followup:
        followup_text = prompt[1:].lstrip()
        if claude_code_mgr.stream_mode_active(chat_id):
            sent = await claude_code_mgr.send_followup(chat_id, followup_text)
            if sent:
                await update.message.reply_text("Sent to Claude.")
                return
        # No active stream (or feed failed) — strip the dash and dispatch normally
        prompt = followup_text

    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return

    # Restore session ID for this (chat, repo) from DB if we don't have one
    # in memory yet, so the auto-started stream resumes via --resume.
    if claude_code_mgr.get_session_id(chat_id, repo) is None:
        saved_session = load_session_id(chat_id, repo)
        if saved_session:
            claude_code_mgr._sessions[(chat_id, repo)] = saved_session
            logger.info("Restored session %s for chat %d on %s", saved_session, chat_id, repo)

    # Auto-start stream if not running. Reader task may have died, in which
    # case stream_mode_active() returns False even when chat_id is in _stream_mode.
    if chat_id not in _stream_mode or not claude_code_mgr.stream_mode_active(chat_id):
        _stream_mode.discard(chat_id)
        err = await _start_stream_for_chat(chat_id, repo, context.bot)
        if err:
            await update.message.reply_text(err)
            return

    sent = await claude_code_mgr.feed(chat_id, prompt)
    if not sent:
        # Stream pipe broken mid-send. Tear down and restart once, then retry.
        await claude_code_mgr.stop_stream(chat_id, kill_proc=True)
        _stream_mode.discard(chat_id)
        err = await _start_stream_for_chat(chat_id, repo, context.bot)
        if err:
            await update.message.reply_text(f"Stream restart failed: {err}")
            return
        sent = await claude_code_mgr.feed(chat_id, prompt)
        if not sent:
            await update.message.reply_text("Stream died and could not be revived. Try /newstream.")
            return

    # Persist session ID once the stream is feeding so it survives restarts.
    current_session = claude_code_mgr.get_session_id(chat_id, repo)
    if current_session:
        save_session_id(chat_id, repo, current_session)

    _start_stream_typing(chat_id, context.bot)


# ── Startup ───────────────────────────────────────────────────────────


WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))  # 0 = disabled


async def notify_startup(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            ("cancel", "Soft interrupt (keeps session)"),
            ("repo", "Set active GitHub repo"),
            ("files", "Browse and download workspace files"),
            ("newstream", "Wipe this repo's session and restart fresh"),
            ("restart", "Update Claude CLI and resume this repo's session"),
            ("model", "Show or change AI model"),
            ("plan", "Toggle plan mode / plan a task"),
            ("branch", "Set active branch"),
            ("work", "Exit plan mode"),
            ("stop", "Stop current work (kills CC process)"),
            ("logs", "View recent bot logs"),
            ("version", "Show bot version"),
            ("btw", "Ask a side question"),
            ("help", "Show help message"),
        ]
    )
    if not ALLOWED_USER_IDS:
        return
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"Teleclaude Agent v{VERSION} started at {now}\nModel: {DEFAULT_MODEL}\nCLI: {claude_code_mgr.cli_path}"

    # Start webhook server if configured
    if WEBHOOK_PORT:
        try:
            from webhooks import start_webhook_server

            runner = await start_webhook_server(app.bot, ALLOWED_USER_IDS, WEBHOOK_PORT)
            app.bot_data["webhook_runner"] = runner
            msg += f"\nWebhooks: port {WEBHOOK_PORT}"
        except Exception as e:
            logger.error("Failed to start webhook server: %s", e)

    # Start credentials sync server if configured
    if CREDENTIALS_PORT and CREDENTIALS_SYNC_TOKEN:
        try:
            from persistence import save_claude_credentials
            from webhooks import start_credentials_server

            def _on_credentials_update(session_key: str, org_id: str) -> None:
                save_claude_credentials(session_key, org_id)
                logger.info("Credentials updated via sync endpoint")

            runner = await start_credentials_server(CREDENTIALS_SYNC_TOKEN, _on_credentials_update, CREDENTIALS_PORT)
            app.bot_data["credentials_runner"] = runner
            msg += f"\nCredentials sync: port {CREDENTIALS_PORT}"
        except Exception as e:
            logger.error("Failed to start credentials sync server: %s", e)

    for user_id in ALLOWED_USER_IDS:
        try:
            repo = get_active_repo(user_id)
            user_msg = msg
            if repo:
                branch = get_active_branch(user_id)
                user_msg += f"\nActive repo: {repo}" + (f" ({branch})" if branch else "")
            await app.bot.send_message(chat_id=user_id, text=user_msg)
        except Exception as e:
            logger.warning("Could not notify user %d: %s", user_id, e)


def main() -> None:
    _check_required_config()
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("repo", set_repo))
    app.add_handler(CommandHandler("branch", set_branch))
    app.add_handler(CommandHandler("stop", stop_work))
    app.add_handler(CommandHandler("cancel", cancel_work))
    app.add_handler(CommandHandler("newstream", new_stream))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("update", update_cli))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CommandHandler("files", list_files))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("version", show_version))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("work", work_command))
    app.add_handler(CommandHandler("btw", btw_command))
    app.add_handler(CommandHandler([str(i) for i in range(1, 6)], repo_shortcut))
    app.add_handler(CallbackQueryHandler(inline_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.PHOTO
                | filters.Document.ALL
                | filters.VOICE
                | filters.Sticker.STATIC
                | filters.LOCATION
                | filters.CONTACT
                | filters.AUDIO
                | filters.VIDEO
                | filters.VIDEO_NOTE
            )
            & ~filters.COMMAND,
            handle_message,
        )
    )

    app.post_init = notify_startup

    logger.info("Teleclaude Agent started — model: %s | cli: %s", DEFAULT_MODEL, claude_code_mgr.cli_path)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
