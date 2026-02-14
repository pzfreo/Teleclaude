"""Teleclaude Agent — every message pipes straight to Claude Code CLI."""

from pathlib import Path as _Path

VERSION = (_Path(__file__).parent / "VERSION").read_text().strip()

import asyncio
import collections
import datetime
import io
import logging
import os
import shutil
import sys
import time

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from claude_code import ClaudeCodeManager
from persistence import (
    audit_log,
    clear_conversation,
    init_db,
    load_active_branch,
    load_active_repo,
    load_conversation,
    load_model,
    save_active_branch,
    save_active_repo,
    save_conversation,
    save_model,
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
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")

AVAILABLE_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-5-20250929",
    "haiku": "claude-haiku-4-5-20251001",
}

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set.")
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
if GITHUB_TOKEN and cli_path:
    claude_code_mgr = ClaudeCodeManager(GITHUB_TOKEN, cli_path=cli_path)
    logger.info("Claude Code CLI: enabled (path=%s)", claude_code_mgr.cli_path)
elif not cli_path:
    logger.error("Claude CLI not found in PATH. Agent bot cannot function.")
    sys.exit(1)
else:
    claude_code_mgr = ClaudeCodeManager(GITHUB_TOKEN, cli_path=cli_path)
    logger.info("Claude Code CLI: enabled without GITHUB_TOKEN")

# ── State ─────────────────────────────────────────────────────────────

active_repos: dict[int, str] = {}
active_branches: dict[int, str] = {}
chat_models: dict[int, str] = {}
_chat_locks: dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)

_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


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
    """Format a tool_use block into a short, readable progress line."""
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
        # Show first 80 chars of command
        short = cmd.split("\n")[0][:80]
        return f"$ {short}" if short else None
    if name == "Glob":
        pattern = inp.get("pattern", "")
        return f"Finding {pattern}" if pattern else None
    if name == "Grep":
        pattern = inp.get("pattern", "")
        return f"Searching: {pattern[:60]}" if pattern else None
    if name == "Task":
        desc = inp.get("description", "")
        return f"Subagent: {desc}" if desc else None
    # Generic fallback
    return name.replace("_", " ").title() if name else None


def _short_path(path: str) -> str:
    """Shorten a file path to last 2-3 components."""
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else path


async def keep_typing(chat, stop_event: asyncio.Event, bot):
    """Keep typing indicator alive until stop_event is set."""
    while not stop_event.is_set():
        try:
            await chat.send_action("typing")
        except TelegramError:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TYPING_INTERVAL)
            break
        except TimeoutError:
            continue


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


# ── Commands ──────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Sorry, you're not authorized to use this bot.")
        return

    repo = get_active_repo(update.effective_chat.id)
    repo_line = f"\nActive repo: {repo}" if repo else ""

    await update.message.reply_text(
        f"Teleclaude Agent — Claude Code on Telegram.{repo_line}\n\n"
        "Every message goes straight to Claude Code CLI.\n\n"
        "Commands:\n"
        "/repo owner/name - Set the active GitHub repo\n"
        "/repo - Show current repo\n"
        "/branch name - Set active branch\n"
        "/new - Start a fresh CLI session\n"
        "/model - Show or switch model (opus/sonnet/haiku)\n"
        "/logs [min] - Download recent logs\n"
        "/version - Show bot version\n"
        "/help - Show this message"
    )


async def set_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    if not context.args:
        repo = get_active_repo(chat_id)
        lines = []
        if repo:
            branch = get_active_branch(chat_id)
            lines.append(f"Active repo: {repo}" + (f" ({branch})" if branch else ""))
            lines.append("")

        if gh_client:
            try:
                loop = asyncio.get_running_loop()
                repos = await loop.run_in_executor(None, gh_client.list_user_repos, 5)
                lines.append("Recent repos:")
                for i, r in enumerate(repos, 1):
                    desc = f" — {r['description']}" if r["description"] else ""
                    marker = " *" if r["full_name"] == repo else ""
                    lines.append(f"  {i}. {r['full_name']}{desc}{marker}")
                lines.append("\n/repo <number> or /repo owner/name")
            except Exception as e:
                logger.warning("Failed to list repos: %s", e)
                if not repo:
                    lines.append("No repo set. Use: /repo owner/name")
        elif not repo:
            lines.append("No repo set. Use: /repo owner/name")

        await update.message.reply_text("\n".join(lines))
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
            await update.message.reply_text("Format: /repo owner/name")
            return

    active_repos[chat_id] = repo
    save_active_repo(chat_id, repo)
    set_active_branch(chat_id, None)
    msg = f"Active repo set to: {repo}\nCloning workspace..."
    await update.message.reply_text(msg)

    async def _clone_notify():
        try:
            await claude_code_mgr.ensure_clone(repo)
            await update.message.reply_text(f"Workspace ready: {repo}")
        except Exception as e:
            logger.error("Clone failed: %s", e)
            await update.message.reply_text(f"Clone failed: {e}")

    asyncio.create_task(_clone_notify())


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


async def new_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    clear_conversation(chat_id)
    set_active_branch(chat_id, None)
    claude_code_mgr.new_session(chat_id)
    await update.message.reply_text("Session cleared. Starting fresh.")


async def show_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    if not context.args:
        model = get_model(chat_id)
        shortcuts = ", ".join(AVAILABLE_MODELS.keys())
        await update.message.reply_text(f"Current model: {model}\nSwitch with: /model <name>\nShortcuts: {shortcuts}")
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


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(f"Teleclaude Agent v{VERSION}\nModel: {get_model(update.effective_chat.id)}")


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

    # Build prompt with attachment paths
    prompt = text
    if attachment_paths:
        paths_str = "\n".join(f"  - {p}" for p in attachment_paths)
        prompt += f"\n\nAttached files (saved to disk, you can read them):\n{paths_str}"

    user_id = update.effective_user.id if update.effective_user else None
    text_preview = (text[:80] + "...") if len(text) > 80 else text
    audit_log("agent_message", chat_id=chat_id, user_id=user_id, detail=text_preview)

    lock = _chat_locks[chat_id]
    if lock.locked():
        try:
            await update.message.reply_text("Queued — finishing current request first.")
        except TelegramError:
            pass
    async with lock:
        await _run_cli(chat_id, prompt, update, context)


async def _run_cli(chat_id: int, prompt: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the prompt through Claude Code CLI and send the result."""
    bot = context.bot
    repo = get_active_repo(chat_id)

    if not repo:
        await update.message.reply_text("No repo set. Use /repo owner/name first.")
        return

    model = get_model(chat_id)
    branch = get_active_branch(chat_id)

    # Progress: send a Telegram message for each tool invocation
    tool_count = 0
    last_progress_time = 0.0
    MIN_PROGRESS_GAP = 2.0  # don't spam faster than every 2s

    async def on_progress(block: dict):
        nonlocal tool_count, last_progress_time
        tool_count += 1
        now = time.time()
        # Throttle: skip if too soon after last message
        if now - last_progress_time < MIN_PROGRESS_GAP:
            return
        line = _format_tool_progress(block)
        if not line:
            return
        last_progress_time = now
        try:
            await bot.send_message(chat_id=chat_id, text=f"[{tool_count}] {line}")
        except TelegramError:
            pass

    # Typing indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(update.effective_chat, stop_typing, bot))

    try:
        result = await claude_code_mgr.run(
            chat_id=chat_id,
            repo=repo,
            prompt=prompt,
            branch=branch,
            model=model,
            on_progress=on_progress,
        )
    except Exception as e:
        logger.error("Claude Code run failed: %s", e, exc_info=True)
        result = f"Claude Code error: {e}"
    finally:
        stop_typing.set()
        await typing_task

    if not result:
        result = "(no output)"

    # Save to conversation history for persistence
    save_conversation(
        chat_id,
        [
            *load_conversation(chat_id),
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": result},
        ],
    )

    await send_long_message(chat_id, result, bot)


# ── Startup ───────────────────────────────────────────────────────────


WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))  # 0 = disabled


async def notify_startup(app: Application) -> None:
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
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("repo", set_repo))
    app.add_handler(CommandHandler("branch", set_branch))
    app.add_handler(CommandHandler("new", new_conversation))
    app.add_handler(CommandHandler("model", show_model))
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CommandHandler("version", show_version))
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
