"""Teleclaude Codex bot with one-shot exec and opt-in persistent app-server modes.

Trimmed counterpart to bot_agent.py. Deliberately does NOT include: autocompact
(Codex's context window/compaction behavior differs and wasn't scoped here),
the [ASK:] inline-keyboard flow, and /df /cleanup /plan /work /btw. Those are
candidates for a follow-up once this prototype is validated.
"""

from pathlib import Path

VERSION = (Path(__file__).parent / "VERSION").read_text().strip()

import asyncio
import io
import logging
import os
import re
import sys
import time
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from codex_code import (
    CodexAppServerManager,
    CodexCodeManager,
    CodexTurnAborted,
    format_item_progress,
    get_codex_cli_version,
    looks_like_auth_error,
    update_codex_cli,
)
from persistence import (
    audit_log,
    init_db,
    load_codex_active_branch,
    load_codex_active_repo,
    load_codex_session_id,
    save_codex_active_branch,
    save_codex_active_repo,
    save_codex_session_id,
)
from shared import (
    download_telegram_file,
    send_long_message,
    setup_logging,
)
from shared import (
    is_authorized as _is_authorized,
)

load_dotenv(".env.codex")
load_dotenv()

_ring_handler = setup_logging()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DEFAULT_MODEL = os.getenv("CODEX_MODEL", "")  # empty = let the CLI use its own default

MAX_TELEGRAM_LENGTH = 4096
TYPING_INTERVAL = 4
MAX_PROGRESS_LINES = 6
MAX_QUEUED_PROMPTS = 5  # messages allowed to wait behind the running turn
CODEX_IDLE_HEARTBEAT_SECONDS = max(1, int(os.getenv("CODEX_IDLE_HEARTBEAT_SECONDS", "600")))

MAX_FILE_BYTES = 50 * 1024 * 1024
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
SEND_MARKER_RE = re.compile(r"\[SEND:\s*([^\]]+)\]", re.IGNORECASE)
LOCAL_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\n]+)\)")

ALLOWED_USER_IDS: set[int] = set()
for uid in os.getenv("ALLOWED_USER_IDS", "").split(","):
    uid = uid.strip()
    if uid.isdigit():
        ALLOWED_USER_IDS.add(int(uid))


def is_authorized(user_id: int) -> bool:
    return _is_authorized(user_id, ALLOWED_USER_IDS)


def _check_required_config() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        sys.exit(1)
    if not codex_mgr.available:
        logger.error("Codex CLI not found in PATH. Codex bot cannot function.")
        sys.exit(1)


if not GITHUB_TOKEN:
    logger.warning("GITHUB_TOKEN is not set — Codex will have no GitHub access.")

# ── Codex CLI ─────────────────────────────────────────────────────────

codex_mgr = CodexCodeManager(GITHUB_TOKEN)
app_server_mgr = CodexAppServerManager(codex_mgr)
if codex_mgr.available:
    logger.info("Codex CLI: enabled (path=%s)", codex_mgr.cli_path)

# GitHub client (for /repo listing only)
gh_client = None
try:
    from github_tools import GitHubClient

    if GITHUB_TOKEN:
        gh_client = GitHubClient(GITHUB_TOKEN)
        logger.info("GitHub client: enabled (for /repo listing)")
except Exception as e:
    logger.warning("GitHub client: failed to load (%s)", e)

# ── State ─────────────────────────────────────────────────────────────

active_repos: dict[int, str] = {}
active_branches: dict[int, str] = {}
chat_models: dict[int, str] = {}
_chat_locks: dict[int, asyncio.Lock] = {}
_typing_tasks: dict[int, asyncio.Task] = {}
_progress_msg_ids: dict[int, int] = {}
_progress_lines: dict[int, list[str]] = {}
_files_cache: dict[int, list[Path]] = {}
_stream_mode: set[int] = set()  # opt-in app-server chats; codex exec remains the default
_stream_control_active: set[int] = set()  # non-turn app-server controls such as /status and /goal
_prompt_active: set[int] = set()  # chat locks currently owned by the prompt runner, not // controls
_pending_steers: dict[int, set[object]] = {}  # follow-ups waiting for turn/started
# Messages that arrived mid-turn, waiting to be handed to Codex together. Whoever
# holds the chat lock drains this when its turn ends; /cancel and /stop clear it,
# so stopping a turn doesn't just start the next queued message.
_pending_prompts: dict[int, list[tuple[str, Update]]] = {}


def get_active_repo(chat_id: int) -> str | None:
    if chat_id not in active_repos:
        saved = load_codex_active_repo(chat_id)
        if saved:
            active_repos[chat_id] = saved
    return active_repos.get(chat_id)


def set_active_repo(chat_id: int, repo: str) -> None:
    active_repos[chat_id] = repo
    save_codex_active_repo(chat_id, repo)


def get_active_branch(chat_id: int) -> str | None:
    if chat_id not in active_branches:
        saved = load_codex_active_branch(chat_id)
        if saved:
            active_branches[chat_id] = saved
    return active_branches.get(chat_id)


def set_active_branch(chat_id: int, branch: str | None) -> None:
    if branch:
        active_branches[chat_id] = branch
    else:
        active_branches.pop(chat_id, None)
    save_codex_active_branch(chat_id, branch)


def get_model(chat_id: int) -> str | None:
    return chat_models.get(chat_id) or DEFAULT_MODEL or None


def _chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


def _drop_queued(chat_id: int) -> int:
    """Discard everything queued for this chat. Returns how many were dropped.

    Without this, stopping a turn just feeds the queued messages to Codex
    straight away, which reads as the cancel not having worked.
    """
    queued = len(_pending_prompts.pop(chat_id, []))
    waiting = len(_pending_steers.pop(chat_id, set()))
    return queued + waiting


# ── Progress / typing UX (mirrors bot_agent.py's ephemeral progress message) ──


async def _update_progress(chat_id: int, line: str, bot) -> None:
    lines = _progress_lines.setdefault(chat_id, [])
    lines.append(line)
    if len(lines) > MAX_PROGRESS_LINES:
        del lines[: len(lines) - MAX_PROGRESS_LINES]
    text = "\n".join(lines)

    msg_id = _progress_msg_ids.get(chat_id)
    if msg_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            _progress_msg_ids.pop(chat_id, None)
        except TelegramError:
            _progress_msg_ids.pop(chat_id, None)
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text, disable_notification=True)
        _progress_msg_ids[chat_id] = msg.message_id
    except TelegramError:
        pass


async def _clear_progress(chat_id: int, bot) -> None:
    msg_id = _progress_msg_ids.pop(chat_id, None)
    _progress_lines.pop(chat_id, None)
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except TelegramError:
            pass


def _start_typing(chat_id: int, bot) -> None:
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


def _stop_typing(chat_id: int) -> None:
    task = _typing_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def _list_workspace_files(workspace: Path, limit: int = 5) -> list[Path]:
    files: list[Path] = []
    try:
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(workspace)
            if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if rel.parts[-1].startswith("."):
                continue
            files.append(path)
    except PermissionError:
        pass
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:limit]


_AUTH_ERROR_HELP = (
    "🔑 Codex authentication failed.\n\n"
    "The bot's Codex CLI credentials have expired or are invalid, so it can't reach OpenAI. "
    "Every message will fail until this is fixed.\n\n"
    "To fix:\n"
    "• Subscription login: re-authenticate the CLI (`codex login`) and restart the bot so the "
    "refreshed token in the mounted .codex volume is picked up.\n"
    "• API key: set a valid CODEX_API_KEY (or run `codex login --api-key`) and restart.\n\n"
    "Then send your message again."
)


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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_send_path(raw: str, workspace: Path | None) -> Path | None:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    parsed = urlparse(target)
    if parsed.scheme == "file":
        target = parsed.path
    elif parsed.scheme:
        return None
    target = unquote(target)
    if not target:
        return None

    path = Path(target)
    if not path.is_absolute() and workspace:
        return (workspace / target).resolve()
    return path.resolve()


async def _send_resolved_path(
    chat_id: int,
    raw: str,
    workspace: Path | None,
    allowed_roots: list[Path],
    bot,
    must_exist: bool = False,
) -> bool:
    path = _resolve_send_path(raw, workspace)
    if path is None:
        return False
    safe = any(_is_relative_to(path, root) for root in allowed_roots)
    if not safe:
        logger.warning("Blocked file send outside workspace: %s", path)
        return True
    if must_exist and not path.exists():
        return False
    await _send_file_to_user(chat_id, path, bot)
    return True


async def _parse_and_send_markers(chat_id: int, text: str, repo: str | None, bot) -> str:
    """Strip local file-send references from text and deliver the referenced files."""
    markers = SEND_MARKER_RE.findall(text)
    workspace = codex_mgr.workspace_path(repo) if repo else None
    shared = (codex_mgr.workspace_root / ".shared" / str(chat_id)).resolve()
    allowed_roots = [Path("/tmp").resolve(), shared]
    if workspace:
        allowed_roots.append(workspace.resolve())
    for raw in markers:
        await _send_resolved_path(chat_id, raw, workspace, allowed_roots, bot)

    text = SEND_MARKER_RE.sub("", text)
    out: list[str] = []
    cursor = 0
    for match in LOCAL_MARKDOWN_LINK_RE.finditer(text):
        raw_target = match.group(2)
        if not await _send_resolved_path(chat_id, raw_target, workspace, allowed_roots, bot, must_exist=True):
            continue
        out.append(text[cursor : match.start()])
        out.append(match.group(1))
        cursor = match.end()
    if not out:
        return text.strip()
    out.append(text[cursor:])
    return "".join(out).strip()


def _make_event_handler(chat_id: int, bot, activity_event: asyncio.Event | None = None):
    """Build an on_event callback that renders Codex exec JSON events into Telegram."""
    state = {"final_text": None, "usage": None}
    pending_agent_message: str | None = None

    async def flush_pending_agent_message() -> None:
        nonlocal pending_agent_message
        if pending_agent_message:
            await send_long_message(chat_id, pending_agent_message, bot, disable_notification=True)
            if state["final_text"] == pending_agent_message:
                state["final_text"] = None
            pending_agent_message = None

    async def on_event(event: dict) -> None:
        nonlocal pending_agent_message
        if activity_event is not None:
            activity_event.set()
        event_type = event.get("type")

        if event_type == "item.started":
            await flush_pending_agent_message()
            line = format_item_progress(event.get("item", {}))
            if line:
                await _update_progress(chat_id, line, bot)
            return

        if event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                state["final_text"] = text
                pending_agent_message = text
                return
            if item.get("type") == "error":
                logger.warning("Codex item error for chat %d: %s", chat_id, item.get("message"))
                return
            await flush_pending_agent_message()
            line = format_item_progress(item)
            if line:
                await _update_progress(chat_id, line, bot)
            return

        if event_type == "turn.completed":
            state["usage"] = event.get("usage")
            return

        if event_type in ("turn.failed", "_process_error"):
            message = event.get("stderr") or (event.get("error") or {}).get("message") or "unknown error"
            await _clear_progress(chat_id, bot)
            if looks_like_auth_error(message):
                logger.error("Codex auth failure for chat %d: %s", chat_id, message)
                await send_long_message(chat_id, _AUTH_ERROR_HELP, bot)
            else:
                logger.error("Codex turn failed for chat %d: %s", chat_id, message)
                await send_long_message(chat_id, f"Codex error: {message[:500]}", bot)
            return

    return on_event, state


async def _idle_heartbeat(chat_id: int, bot, activity_event: asyncio.Event) -> None:
    """Post a durable status update when Codex emits no events for a while."""
    while True:
        activity_event.clear()
        try:
            await asyncio.wait_for(activity_event.wait(), timeout=CODEX_IDLE_HEARTBEAT_SECONDS)
        except TimeoutError:
            minutes = max(1, round(CODEX_IDLE_HEARTBEAT_SECONDS / 60))
            await send_long_message(
                chat_id,
                f"Still working. No new Codex events for {minutes} minute(s). Use /cancel to interrupt.",
                bot,
                disable_notification=True,
            )


# ── Commands ──────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return
    repo = get_active_repo(update.effective_chat.id)
    repo_line = f"\nActive repo: {repo}" if repo else ""
    await update.message.reply_text(
        f"Teleclaude Codex (prototype) — Codex CLI on Telegram.{repo_line}\n\n"
        "Commands:\n"
        "/repo - Show current repo + recent repos to tap\n"
        "/repo <number> - Pick from the recent list\n"
        "/repo <name> - Search local clones + GitHub by substring\n"
        "/repo owner/name - Set the active GitHub repo directly\n"
        "/branch name - Set active branch\n"
        "/newsession - Wipe this repo's session and start fresh\n"
        "/stream - Use persistent Codex app-server mode (experimental)\n"
        "/nostream - Return to one-shot codex exec mode\n"
        "/status, /usage, /compact, /goal - App-server controls in stream mode\n"
        "//command - Pass /command through to Codex as turn input\n"
        "/cancel - Stop the active turn and clear the queue\n"
        "/stop - Force-kill the Codex run and its child processes now\n"
        "/model [name] - Show or switch model\n"
        "/files - Browse and download workspace files\n"
        "/update - Update Codex CLI to latest version\n"
        "/logs [min] - Download recent logs\n"
        "/version - Show bot version\n"
        "/help - Show this message"
    )


def _find_repo_candidates(name: str, limit: int = 5) -> list[str]:
    """Resolve a bare repo name to up to `limit` 'owner/name' candidates.

    Looks first at locally cloned repos under workspaces-codex/pzfreo/, then at
    the GitHub user's most-recently-pushed repos. Case-insensitive substring
    match. Local matches are preferred and listed first.
    """
    needle = name.lower()
    seen: set[str] = set()
    candidates: list[str] = []

    local_root = codex_mgr.workspace_root / "pzfreo"
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


def _switch_repo(chat_id: int, repo: str) -> None:
    """Set the active repo. Sessions are per-(chat, repo), so switching does
    NOT clear the target repo's stored session — memory resumes if you switch
    back to a repo you'd used before."""
    set_active_repo(chat_id, repo)
    set_active_branch(chat_id, None)


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

    _switch_repo(chat_id, repo)
    await update.message.reply_text(f"Active repo set to: {repo}")


async def inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks: dl:, repo:"""
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
        _switch_repo(chat_id, repo)
        await query.edit_message_text(f"Active repo set to: {repo}")


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

    set_active_branch(chat_id, arg)
    msg = f"Active branch set to: {arg}"
    if repo:
        ws = codex_mgr.workspace_path(repo)
        if (ws / ".git").is_dir():
            try:
                await codex_mgr.checkout_branch(repo, arg)
                msg += " (checked out locally)"
            except Exception as e:
                msg += f" (local checkout failed: {e})"
    await update.message.reply_text(msg)


async def new_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return
    await app_server_mgr.stop(chat_id)
    codex_mgr.new_session(chat_id, repo)
    save_codex_session_id(chat_id, repo, None)
    await update.message.reply_text(f"Session cleared for {repo}. Next message starts fresh.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    _stop_typing(chat_id)
    await _clear_progress(chat_id, context.bot)
    dropped = _drop_queued(chat_id)
    stream_stopped = await app_server_mgr.stop(chat_id, mark_pending=_chat_lock(chat_id).locked())
    exec_stopped = await codex_mgr.abort(chat_id, mark_pending=_chat_lock(chat_id).locked())
    stopped = stream_stopped or exec_stopped
    await update.message.reply_text(_with_dropped("Stopped." if stopped else "Nothing running.", dropped))


_CANCEL_OUTCOMES = {
    "idle": "Nothing running.",
    "cancelled": "Cancelled current turn.",
    "forced": "Codex ignored the interrupt — killed it.",
}


def _with_dropped(text: str, dropped: int) -> str:
    if not dropped:
        return text
    return f"{text} Dropped {dropped} queued message{_s(dropped)}."


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the active Codex turn: SIGINT first, force-kill if it doesn't take."""
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    _stop_typing(chat_id)
    await _clear_progress(chat_id, context.bot)
    # Drop waiters before signalling, so none of them can grab the lock the
    # instant the current turn dies.
    dropped = _drop_queued(chat_id)
    # Acknowledge before signalling: waiting out the SIGINT grace period can take
    # seconds, and a silent /cancel reads as a bot that ignored the command.
    ack = await update.message.reply_text("Interrupting Codex…")
    if chat_id in _stream_mode:
        outcome = (
            "idle"
            if chat_id in _stream_control_active
            else await app_server_mgr.interrupt(chat_id, mark_pending=_chat_lock(chat_id).locked())
        )
    else:
        outcome = await codex_mgr.interrupt(chat_id, mark_pending=_chat_lock(chat_id).locked())
    text = _with_dropped(_CANCEL_OUTCOMES[outcome], dropped)
    try:
        await ack.edit_text(text)
    except TelegramError:
        await update.message.reply_text(text)


async def stream_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opt this chat into the persistent Codex app-server prototype."""
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if _chat_lock(chat_id).locked():
        await update.message.reply_text("Codex is working. Use /cancel before switching modes.")
        return
    if chat_id in _stream_mode:
        await update.message.reply_text("Persistent stream mode is already enabled.")
        return
    _stream_mode.add(chat_id)
    await update.message.reply_text(
        "Persistent stream mode enabled (experimental). The next message will use Codex app-server."
    )


async def nostream_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return this chat to the established one-shot codex exec transport."""
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if _chat_lock(chat_id).locked():
        await update.message.reply_text("Codex is working. Use /cancel before switching modes.")
        return
    was_streaming = chat_id in _stream_mode
    _stream_mode.discard(chat_id)
    await app_server_mgr.stop(chat_id)
    await update.message.reply_text(
        "One-shot mode enabled. Future messages will use codex exec."
        if was_streaming
        else "One-shot mode is already enabled."
    )


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        model = get_model(chat_id) or "(CLI default)"
        await update.message.reply_text(f"Current model: {model}\n/model <name> to switch")
        return
    model_id = context.args[0]
    chat_models[chat_id] = model_id
    await update.message.reply_text(f"Model switched to: {model_id}")


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return
    workspace = codex_mgr.workspace_path(repo)
    if not workspace.exists():
        await update.message.reply_text("Workspace not cloned yet. Send a message after setting a repo first.")
        return
    files = _list_workspace_files(workspace)
    if not files:
        await update.message.reply_text("No files found in workspace.")
        return

    _files_cache[chat_id] = files
    buttons = []
    for idx, path in enumerate(files):
        rel = path.relative_to(workspace)
        size = path.stat().st_size
        size_str = f"{size / 1024:.1f}KB" if size >= 1024 else f"{size}B"
        buttons.append([InlineKeyboardButton(f"{rel}  ({size_str})", callback_data=f"dl:{idx}")])
    await update.message.reply_text(
        "Recent files - tap to download:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def update_cli(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("Checking for Codex CLI updates...")
    success, msg = await update_codex_cli()
    if success:
        await update.message.reply_text(f"✅ Codex CLI {msg}")
    else:
        version = await get_codex_cli_version()
        status = f"Current version: {version}\n" if version else ""
        await update.message.reply_text(f"{status}Info: {msg}")


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    cli_version = await get_codex_cli_version()
    await update.message.reply_text(f"Teleclaude Codex bot v{VERSION}\nCodex CLI: {cli_version or 'unknown'}")


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
    buf.name = f"codex_logs_{minutes}min.txt"
    await update.message.reply_document(document=buf, caption=f"Last {minutes} min — {len(lines)} lines")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("Unknown command. /help for a list.")


# ── Message handling ──────────────────────────────────────────────────


def _save_attachment(chat_id: int, data: bytes, mime: str, label: str = "") -> str:
    shared_dir = codex_mgr.workspace_root / ".shared" / str(chat_id)
    shared_dir.mkdir(parents=True, exist_ok=True)
    ext = _MIME_TO_EXT.get(mime, "")
    safe_label = label.replace("/", "_").replace("\\", "_").replace("..", "_")
    name = f"{safe_label}_{int(time.time())}{ext}" if safe_label else f"{int(time.time())}{ext}"
    path = (shared_dir / name).resolve()
    if not str(path).startswith(str(shared_dir.resolve())):
        raise ValueError(f"Path traversal blocked in attachment save: {name!r}")
    path.write_bytes(data)
    return str(path)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    chat_id = update.effective_chat.id
    msg = update.message
    text = msg.text or msg.caption or ""
    attachment_paths = []

    if msg.photo:
        try:
            data = await download_telegram_file(msg.photo[-1], context.bot)
            attachment_paths.append(_save_attachment(chat_id, data, "image/jpeg", "photo"))
        except Exception as e:
            logger.warning("Failed to download photo: %s", e)

    if msg.document:
        mime = msg.document.mime_type or ""
        fname = msg.document.file_name or "file"
        try:
            data = await download_telegram_file(msg.document, context.bot)
            attachment_paths.append(_save_attachment(chat_id, data, mime, fname.rsplit(".", 1)[0]))
        except Exception as e:
            logger.warning("Failed to download document: %s", e)
            text += f"\n[Attached file: {fname} — download failed]"

    if not text and not attachment_paths:
        return

    if text.startswith("//"):
        text = text[1:]

    prompt = text
    if attachment_paths:
        paths_str = "\n".join(f"  - {p}" for p in attachment_paths)
        prompt += f"\n\nAttached files (saved to disk, you can read them):\n{paths_str}"

    await _queue_prompt(chat_id, prompt, update, context)


async def _handle_stream_slash(
    chat_id: int,
    command: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Map fixed Telegram control commands onto explicit app-server operations."""
    # Clear only stale state from an earlier operation. A /cancel arriving after
    # this synchronous boundary remains armed throughout repository preparation.
    app_server_mgr.clear_pending_interrupt(chat_id)
    lock = _chat_lock(chat_id)
    if lock.locked():
        await update.message.reply_text("Codex is working. Wait for it to finish or use /cancel first.")
        return
    async with lock:
        repo = get_active_repo(chat_id)
        if not repo:
            await update.message.reply_text("No repo set. Use /repo owner/name first.")
            return
        if codex_mgr.get_session_id(chat_id, repo) is None:
            saved_session = load_codex_session_id(chat_id, repo)
            if saved_session:
                codex_mgr._sessions[(chat_id, repo)] = saved_session
        command_name = command.lstrip("/").split(None, 1)[0].lower() if command.strip("/").strip() else ""
        is_control = command_name in {"goal", "status", "usage"}
        if is_control:
            _stream_control_active.add(chat_id)
        try:
            await codex_mgr.ensure_clone(repo)
            branch = get_active_branch(chat_id)
            if branch:
                await codex_mgr.checkout_branch(repo, branch)
            response = await app_server_mgr.execute_slash(chat_id, repo, command, model=get_model(chat_id))
            thread_id = codex_mgr.get_session_id(chat_id, repo)
            if thread_id:
                save_codex_session_id(chat_id, repo, thread_id)
        except CodexTurnAborted:
            logger.info("Codex stream command stopped for chat %d", chat_id)
            return
        except Exception as exc:
            logger.error("Codex stream command failed for chat %d: %s", chat_id, exc, exc_info=True)
            await update.message.reply_text(f"Codex stream command failed: {exc}")
            return
        finally:
            if is_control:
                _stream_control_active.discard(chat_id)
    await send_long_message(chat_id, response, context.bot)


async def stream_control_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a fixed app-server operation exposed as a Telegram command."""
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if chat_id not in _stream_mode:
        await update.message.reply_text("This command requires stream mode. Use /stream first.")
        return
    command_name = (update.message.text or "").split(None, 1)[0].split("@", 1)[0]
    args = " ".join(context.args)
    command = f"{command_name} {args}".rstrip()
    await _handle_stream_slash(chat_id, command, update, context)


async def _queue_prompt(chat_id: int, prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the prompt now, or park it for the running turn to pick up when it ends."""
    msg = update.message
    lock = _chat_lock(chat_id)

    if lock.locked():
        if chat_id in _stream_mode:
            try:
                if await app_server_mgr.steer(chat_id, prompt):
                    audit_log(
                        "codex_message_steered",
                        chat_id=chat_id,
                        user_id=update.effective_user.id if update.effective_user else None,
                        detail=(prompt[:80] + "...") if len(prompt) > 80 else prompt,
                    )
                    await msg.reply_text("Added to the active Codex turn.")
                    return
            except Exception as exc:
                # A turn can finish between observing its id and the server
                # accepting turn/steer. Do not enqueue after a failed steer:
                # /cancel may already have cleared the queue in that window.
                logger.warning("Codex turn/steer failed for chat %d: %s", chat_id, exc)
                await msg.reply_text("Could not add that message to the active turn. Please send it again.")
                return
            if chat_id in _prompt_active:
                waiting = _pending_steers.setdefault(chat_id, set())
                if len(waiting) + len(_pending_prompts.get(chat_id, [])) >= MAX_QUEUED_PROMPTS:
                    await msg.reply_text(
                        f"{MAX_QUEUED_PROMPTS} messages are already waiting. "
                        "Use /cancel to stop the current turn and clear them."
                    )
                    return
                token = object()
                waiting.add(token)
                await msg.reply_text("Waiting for the active Codex turn to accept this message…")
                await _wait_and_steer(chat_id, prompt, update, context, token)
                return
            else:
                # Stream control commands share the lock but do not drain the
                # prompt queue. Wait for that command, then dispatch normally.
                await msg.reply_text("Waiting for the current Codex command to finish…")
                async with lock:
                    pass
                await _queue_prompt(chat_id, prompt, update, context)
                return
        pending = _pending_prompts.setdefault(chat_id, [])
        if len(pending) >= MAX_QUEUED_PROMPTS:
            await msg.reply_text(
                f"{MAX_QUEUED_PROMPTS} messages are already waiting. "
                "Use /cancel to stop the current turn and clear the queue."
            )
            return
        # Appended before the ack below, so nothing can be lost to that await.
        pending.append((prompt, update))
        await msg.reply_text(f"Queued (#{len(pending)}) — goes to Codex when this turn finishes.")
        return

    dropped = 0
    async with lock:
        _prompt_active.add(chat_id)
        try:
            keep_going = await _dispatch_prompt(chat_id, prompt, update, context)
            while keep_going:
                queued = _pending_prompts.pop(chat_id, None)
                if not queued:
                    break
                # One turn for the lot: `codex exec resume` reloads the thread each time,
                # so N follow-ups as N turns is N times the setup for the same context.
                logger.info("Codex: merging %d queued message(s) into one turn for chat %d", len(queued), chat_id)
                merged = "\n\n".join(text for text, _ in queued)
                keep_going = await _dispatch_prompt(chat_id, merged, queued[-1][1], context)

            if not keep_going:
                # Aborted mid-turn: anything queued in the gap before it actually
                # stopped belongs to the work the user just cancelled.
                dropped = _drop_queued(chat_id)
        finally:
            _prompt_active.discard(chat_id)

    # Everything above the lock release is synchronous once the queue reads empty,
    # so a message arriving from here on finds an unlocked chat and runs itself
    # rather than being parked with no one left to drain it.
    if dropped:
        await _notify_dropped(chat_id, dropped, context.bot)


async def _wait_and_steer(
    chat_id: int,
    prompt: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token: object,
) -> None:
    """Steer a follow-up once turn/started arrives, or run it after a normal early exit."""
    while token in _pending_steers.get(chat_id, set()):
        try:
            if await app_server_mgr.steer(chat_id, prompt):
                waiting = _pending_steers.get(chat_id)
                if not waiting or token not in waiting:
                    return  # /cancel removed it while the request was in flight
                waiting.discard(token)
                if not waiting:
                    _pending_steers.pop(chat_id, None)
                audit_log(
                    "codex_message_steered",
                    chat_id=chat_id,
                    user_id=update.effective_user.id if update.effective_user else None,
                    detail=(prompt[:80] + "...") if len(prompt) > 80 else prompt,
                )
                await update.message.reply_text("Added to the active Codex turn.")
                return
        except Exception as exc:
            logger.warning("Deferred Codex turn/steer failed for chat %d: %s", chat_id, exc)
            waiting = _pending_steers.get(chat_id)
            if waiting:
                waiting.discard(token)
                if not waiting:
                    _pending_steers.pop(chat_id, None)
            await update.message.reply_text("Could not add that message to the active turn. Please send it again.")
            return

        if chat_id not in _prompt_active:
            waiting = _pending_steers.get(chat_id)
            if not waiting or token not in waiting:
                return  # cancelled, rather than a normal turn exit
            waiting.discard(token)
            if not waiting:
                _pending_steers.pop(chat_id, None)
            await _queue_prompt(chat_id, prompt, update, context)
            return
        await asyncio.sleep(0.05)


async def _notify_dropped(chat_id: int, dropped: int, bot) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=f"Dropped {dropped} queued message{_s(dropped)}.")
    except TelegramError:
        pass


def _s(count: int) -> str:
    return "" if count == 1 else "s"


async def _dispatch_prompt(chat_id: int, prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Run one Codex turn. Returns False if it was stopped, so the caller
    knows not to feed it whatever was queued behind it."""
    # An abort flag belongs to the turn it was raised against. One left behind by
    # a /stop that landed outside a turn would otherwise cancel this message
    # before it ever reached Codex, silently. Must run before the first await.
    codex_mgr.clear_pending_abort(chat_id)
    app_server_mgr.clear_pending_interrupt(chat_id)

    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return True

    # Drop any progress message the previous turn left behind, so its lines don't
    # bleed into this turn's.
    await _clear_progress(chat_id, context.bot)

    if codex_mgr.get_session_id(chat_id, repo) is None:
        saved_session = load_codex_session_id(chat_id, repo)
        if saved_session:
            codex_mgr._sessions[(chat_id, repo)] = saved_session

    user_id = update.effective_user.id if update.effective_user else None
    text_preview = (prompt[:80] + "...") if len(prompt) > 80 else prompt
    audit_log("codex_message", chat_id=chat_id, user_id=user_id, detail=text_preview)

    try:
        await codex_mgr.ensure_clone(repo)
        branch = get_active_branch(chat_id)
        if branch:
            await codex_mgr.checkout_branch(repo, branch)
        await codex_mgr.pull_latest(repo)
    except Exception as e:
        await update.message.reply_text(f"Failed to prepare workspace: {e}")
        return True

    _start_typing(chat_id, context.bot)
    activity_event = asyncio.Event()
    on_event, state = _make_event_handler(chat_id, context.bot, activity_event)
    heartbeat_task = asyncio.create_task(_idle_heartbeat(chat_id, context.bot, activity_event))
    try:
        manager = app_server_mgr if chat_id in _stream_mode else codex_mgr
        await manager.run_turn(chat_id, repo, prompt, on_event, model=get_model(chat_id))
    except CodexTurnAborted:
        logger.info("Codex turn stopped for chat %d", chat_id)
        # The reader may have posted progress after /cancel cleared it.
        await _clear_progress(chat_id, context.bot)
        return False
    except Exception as e:
        logger.error("Codex run_turn failed for chat %d: %s", chat_id, e, exc_info=True)
        await update.message.reply_text(f"Codex Code error: {e}")
        return True
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        _stop_typing(chat_id)

    new_session_id = codex_mgr.get_session_id(chat_id, repo)
    if new_session_id:
        save_codex_session_id(chat_id, repo, new_session_id)

    await _clear_progress(chat_id, context.bot)
    final_text = state["final_text"]
    if final_text:
        final_text = await _parse_and_send_markers(chat_id, final_text, repo, context.bot)
        if final_text:
            await send_long_message(chat_id, final_text, context.bot)
    return True


async def notify_startup(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            ("repo", "Set active GitHub repo (list / number / name / owner/name)"),
            ("branch", "Set active branch"),
            ("newsession", "Wipe this repo's session and start fresh"),
            ("stream", "Enable persistent app-server mode (experimental)"),
            ("nostream", "Return to one-shot codex exec mode"),
            ("status", "Show Codex app-server status (stream mode)"),
            ("usage", "Show Codex account usage (stream mode)"),
            ("compact", "Compact Codex context (stream mode)"),
            ("goal", "Show or set the Codex goal (stream mode)"),
            ("cancel", "Stop the active turn and clear the queue"),
            ("stop", "Force-kill Codex run and child processes now"),
            ("model", "Show or switch model"),
            ("files", "Browse and download workspace files"),
            ("update", "Update Codex CLI to latest version"),
            ("logs", "View recent bot logs"),
            ("version", "Show bot version"),
            ("help", "Show help message"),
        ]
    )


async def shutdown_app_server(_app: Application) -> None:
    await app_server_mgr.stop_all()


def main() -> None:
    _check_required_config()
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("repo", set_repo))
    app.add_handler(CommandHandler("branch", set_branch))
    app.add_handler(CommandHandler("newsession", new_session_command))
    app.add_handler(CommandHandler("stream", stream_command))
    app.add_handler(CommandHandler("nostream", nostream_command))
    app.add_handler(CommandHandler(["status", "usage", "compact", "goal"], stream_control_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(CommandHandler("files", list_files))
    app.add_handler(CommandHandler("update", update_cli))
    app.add_handler(CommandHandler("version", show_version))
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CallbackQueryHandler(inline_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
            handle_message,
        )
    )

    app.post_init = notify_startup
    app.post_shutdown = shutdown_app_server

    logger.info("Teleclaude Codex bot started — cli: %s", codex_mgr.cli_path)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
