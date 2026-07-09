"""Teleclaude Codex bot (prototype) — every message pipes to Codex CLI via `codex exec`.

Trimmed counterpart to bot_agent.py. Deliberately does NOT include: autocompact
(Codex's context window/compaction behavior differs and wasn't scoped here),
the rtk context-compression hook (Claude-Code-specific), persistent stream
mode (codex_code.py has no equivalent — see its module docstring), the
[ASK:] inline-keyboard flow, and /files /df /cleanup /plan /work /btw. Those
are candidates for a follow-up once this prototype is validated.
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


async def _parse_and_send_markers(chat_id: int, text: str, repo: str | None, bot) -> str:
    """Strip [SEND: path] markers from text and deliver the referenced files."""
    markers = SEND_MARKER_RE.findall(text)
    workspace = codex_mgr.workspace_path(repo) if repo else None
    shared = (codex_mgr.workspace_root / ".shared" / str(chat_id)).resolve()
    allowed_roots = [Path("/tmp").resolve(), shared]
    if workspace:
        allowed_roots.append(workspace.resolve())
    for raw in markers:
        raw = raw.strip()
        p = Path(raw)
        if not p.is_absolute() and workspace:
            p = (workspace / raw).resolve()
        else:
            p = p.resolve()
        safe = any(_is_relative_to(p, root) for root in allowed_roots)
        if safe:
            await _send_file_to_user(chat_id, p, bot)
        else:
            logger.warning("Blocked file send outside workspace: %s", p)
    return SEND_MARKER_RE.sub("", text).strip()


def _make_event_handler(chat_id: int, bot):
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
        "/stop - Kill any in-flight Codex run\n"
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
    codex_mgr.new_session(chat_id, repo)
    save_codex_session_id(chat_id, repo, None)
    await update.message.reply_text(f"Session cleared for {repo}. Next message starts fresh.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    _stop_typing(chat_id)
    await _clear_progress(chat_id, context.bot)
    stopped = await codex_mgr.abort(chat_id, mark_pending=_chat_lock(chat_id).locked())
    await update.message.reply_text("Stopped." if stopped else "Nothing running.")


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

    prompt = text
    if attachment_paths:
        paths_str = "\n".join(f"  - {p}" for p in attachment_paths)
        prompt += f"\n\nAttached files (saved to disk, you can read them):\n{paths_str}"

    async with _chat_lock(chat_id):
        await _dispatch_prompt(chat_id, prompt, update, context)


async def _dispatch_prompt(chat_id: int, prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    repo = get_active_repo(chat_id)
    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return

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
        return

    _start_typing(chat_id, context.bot)
    on_event, state = _make_event_handler(chat_id, context.bot)
    try:
        await codex_mgr.run_turn(chat_id, repo, prompt, on_event, model=get_model(chat_id))
    except CodexTurnAborted:
        logger.info("Codex turn stopped for chat %d", chat_id)
        return
    except Exception as e:
        logger.error("Codex run_turn failed for chat %d: %s", chat_id, e, exc_info=True)
        await update.message.reply_text(f"Codex Code error: {e}")
        return
    finally:
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


async def notify_startup(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            ("repo", "Set active GitHub repo (list / number / name / owner/name)"),
            ("branch", "Set active branch"),
            ("newsession", "Wipe this repo's session and start fresh"),
            ("stop", "Kill any in-flight Codex run"),
            ("model", "Show or switch model"),
            ("files", "Browse and download workspace files"),
            ("update", "Update Codex CLI to latest version"),
            ("logs", "View recent bot logs"),
            ("version", "Show bot version"),
            ("help", "Show help message"),
        ]
    )


def main() -> None:
    _check_required_config()
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("repo", set_repo))
    app.add_handler(CommandHandler("branch", set_branch))
    app.add_handler(CommandHandler("newsession", new_session_command))
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

    logger.info("Teleclaude Codex bot started — cli: %s", codex_mgr.cli_path)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
