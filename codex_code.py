"""Codex CLI integration — routes agent-mode messages through `codex` CLI.

Prototype counterpart to claude_code.py. The default path uses `codex app-server`
over JSONL stdio for persistent threads, streamed events, steering, and
protocol-level interruption. Chats can opt out through `/nostream`, which uses
single-shot `codex exec` subprocesses with continuity through `resume`.

Event schema below (thread.started / turn.started / item.started /
item.completed / turn.completed / turn.failed / error) was verified against a
real `codex exec --json` run (Codex CLI 0.142.5), not just documentation.
Auth-failure phrasing under `_AUTH_ERROR_MARKERS` is a best-effort superset
based on the CLI's other error payloads — it has not been verified against an
actual expired/invalid credential and may need adjusting once seen in the
wild.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 120  # 2 minutes for git operations
NPM_UPDATE_TIMEOUT = 180  # 3 minutes for npm update
PROCESS_ABORT_TIMEOUT = 3  # seconds to wait after TERM before escalating
PROCESS_INTERRUPT_GRACE = 8  # seconds to let Codex wind down after SIGINT before force-killing
PROCESS_EXIT_WAIT = 15  # seconds to wait for exit after Codex closed stdout on its own
_ORPHAN_POLL_INTERVAL = 0.1  # seconds between checks that a signalled process group has drained


class CodexTurnAborted(Exception):
    """Raised when a Codex turn is intentionally stopped by the user."""


@dataclass
class _AppServerConnection:
    proc: asyncio.subprocess.Process
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_id: int = 1
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    active_repo: str | None = None
    active_thread_id: str | None = None
    active_turn_id: str | None = None
    turn_ready: bool = False
    on_event: Any = None
    turn_done: asyncio.Future | None = None
    compact_done: asyncio.Future | None = None


async def update_codex_cli() -> tuple[bool, str]:
    """Update Codex CLI to latest version via npm.

    Returns:
        (success: bool, message: str) - success flag and status message
    """
    npm_path = shutil.which("npm")
    if not npm_path:
        return False, "npm not found"

    logger.info("Checking for Codex CLI updates...")

    try:
        proc = await asyncio.create_subprocess_exec(
            npm_path,
            "install",
            "-g",
            "@openai/codex@latest",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=NPM_UPDATE_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "npm update timed out"

        if proc.returncode != 0:
            err_msg = stderr.decode().strip() or stdout.decode().strip()
            if "EACCES" in err_msg or "permission denied" in err_msg.lower():
                logger.warning("npm update failed (permissions): %s", err_msg)
                return False, "Permission denied (may already be latest)"
            logger.warning("npm update failed: %s", err_msg)
            return False, f"Update failed: {err_msg[:100]}"

        version = await get_codex_cli_version()
        logger.info("Codex CLI updated successfully to %s", version or "unknown")
        return True, f"Updated to {version}" if version else "Updated successfully"

    except Exception as e:
        logger.error("Failed to update Codex CLI: %s", e)
        return False, str(e)


async def get_codex_cli_version() -> str | None:
    """Get the installed Codex CLI version.

    Returns:
        Version string (e.g., "0.142.5") or None if unavailable
    """
    cli_path = shutil.which("codex")
    if not cli_path:
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            cli_path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None

        if proc.returncode == 0:
            version = stdout.decode().strip()
            # Output is "codex-cli 0.142.5"
            if " " in version:
                version = version.rsplit(" ", 1)[-1]
            return version

    except Exception as e:
        logger.warning("Failed to get Codex CLI version: %s", e)

    return None


# ── Progress formatting ────────────────────────────────────────────────

_SHORT_COMMAND_LEN = 120
_SHORT_PROGRESS_TEXT_LEN = 280


def format_agent_progress(text: str | None) -> str | None:
    """Format a public assistant message into a compact progress line."""
    if not text:
        return None
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    first_line = lines[0]
    if len(first_line) > _SHORT_PROGRESS_TEXT_LEN:
        first_line = first_line[:_SHORT_PROGRESS_TEXT_LEN] + "…"
    return first_line


def format_item_progress(item: dict) -> str | None:
    """Format a Codex `item` payload (from item.started/item.completed) into a progress line.

    Returns None for item types that shouldn't be surfaced as progress (e.g.
    agent_message, which is the actual reply text and handled separately by
    the caller).
    """
    item_type = item.get("type")

    if item_type == "command_execution":
        command = item.get("command", "")
        status = item.get("status")
        if status == "in_progress":
            first_line = command.split("\n", 1)[0]
            if len(first_line) > _SHORT_COMMAND_LEN:
                first_line = first_line[:_SHORT_COMMAND_LEN] + "…"
            return f"$ {first_line}"
        if status == "completed":
            exit_code = item.get("exit_code")
            return None if exit_code == 0 else f"$ command exited {exit_code}"
        return None

    if item_type == "file_change":
        path = item.get("path", "")
        return f"Editing {path}" if path else "Editing file"

    if item_type == "mcp_tool_call":
        tool = item.get("tool") or item.get("server", "")
        return f"MCP: {tool}" if tool else "MCP tool call"

    if item_type == "web_search":
        query = item.get("query", "")
        return f"Searching: {query}" if query else "Web search"

    if item_type == "error":
        message = item.get("message", "")
        return f"⚠️ {message}" if message else "⚠️ error"

    # agent_message / reasoning / anything else: not a progress line
    return None


# ── Authentication failure detection ───────────────────────────────────

_AUTH_ERROR_MARKERS = (
    "authentication_error",
    "invalid api key",
    "invalid_api_key",
    "not logged in",
    "please log in",
    "please run `codex login`",
    "run `codex login`",
    "token has expired",
    "token expired",
    "logged out",
)


def looks_like_auth_error(text: str | None) -> bool:
    """True if CLI output text indicates an OpenAI/Codex authentication failure.

    Matches specific auth phrases, plus a bare 401/unauthorized only when it
    appears in an API-error context — avoids false positives on normal text
    that merely mentions the number 401.
    """
    if not text:
        return False
    low = text.lower()
    if any(m in low for m in _AUTH_ERROR_MARKERS):
        return True
    has_401 = "401" in low or "unauthorized" in low
    api_context = "invalid_request_error" in low or "openai" in low or '"status"' in low
    return has_401 and api_context


class CodexCodeManager:
    """Manages local clones and Codex CLI turns.

    Unlike ClaudeCodeManager, there is no persistent subprocess per chat —
    `codex exec` runs one turn and exits; continuity is via `resume`.
    """

    def __init__(self, github_token: str, workspace_root: str | None = None, cli_path: str | None = None):
        self.github_token = github_token
        self.workspace_root = Path(workspace_root or os.getenv("CODEX_CODE_WORKSPACE") or "workspaces-codex")
        self.cli_path = cli_path or os.getenv("CODEX_CLI_PATH") or shutil.which("codex")
        self._sessions: dict[tuple[int, str], str] = {}  # (chat_id, repo) → Codex thread_id
        self._running_procs: dict[int, asyncio.subprocess.Process] = {}  # chat_id → in-flight proc
        self._aborted_chats: set[int] = set()
        self._abort_events: dict[int, asyncio.Event] = {}  # chat_id → signal to stop reading stdout
        self._pgids: dict[int, int] = {}  # chat_id → process group of the last turn we spawned

    @property
    def available(self) -> bool:
        return bool(self.cli_path)

    # ── Workspace management (same approach as ClaudeCodeManager) ────

    def workspace_path(self, repo: str) -> Path:
        """Return local path for a repo clone: workspaces-codex/{owner}/{name}/"""
        owner, name = repo.split("/", 1)
        path = (self.workspace_root / owner / name).resolve()
        root = self.workspace_root.resolve()
        if not str(path).startswith(str(root) + os.sep) and path != root:
            raise ValueError(f"Path traversal blocked: {repo!r} resolves outside workspace root")
        return path

    def _git_env(self) -> dict[str, str]:
        """Build environment for git subprocesses with credential helper."""
        env = os.environ.copy()
        if self.github_token:
            env["GIT_ASKPASS"] = "/bin/sh"
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = (
                f"!f() {{ echo username=x-access-token; echo password={self.github_token}; }}; f"
            )
        return env

    async def ensure_clone(self, repo: str) -> Path:
        """Clone the repo if it doesn't already exist locally. Returns the path."""
        path = self.workspace_path(repo)
        if (path / ".git").is_dir():
            await self._sanitize_remote(path, repo)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{repo}.git"
        await self._git(path.parent, "clone", url, path.name)
        logger.info("Cloned %s to %s", repo, path)
        return path

    async def _sanitize_remote(self, path: Path, repo: str) -> bool:
        """Strip embedded credentials from origin URL of an existing clone."""
        try:
            url = await self._git(path, "remote", "get-url", "origin")
        except RuntimeError:
            return False
        if "@github.com" not in url:
            return False
        clean = f"https://github.com/{repo}.git"
        await self._git(path, "remote", "set-url", "origin", clean)
        logger.warning("Sanitized token-embedded origin URL for %s", repo)
        return True

    async def checkout_branch(self, repo: str, branch: str) -> str:
        """Fetch and checkout a branch in the local clone."""
        cwd = self.workspace_path(repo)
        if not (cwd / ".git").is_dir():
            await self.ensure_clone(repo)
        await self._git(cwd, "fetch", "origin")
        try:
            await self._git(cwd, "checkout", branch)
        except RuntimeError:
            await self._git(cwd, "checkout", "-b", branch, f"origin/{branch}")
        return branch

    async def pull_latest(self, repo: str) -> None:
        """Pull latest changes before a run."""
        cwd = self.workspace_path(repo)
        if not (cwd / ".git").is_dir():
            return
        try:
            await self._git(cwd, "pull", "--ff-only")
        except RuntimeError as e:
            logger.warning("git pull --ff-only failed (expected if local changes): %s", e)

    async def _git(self, cwd: Path, *args: str) -> str:
        """Run a git command as an async subprocess with timeout."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._git_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT}s") from None
        if proc.returncode != 0:
            msg = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
        return stdout.decode().strip()

    # ── Session management ────────────────────────────────────────────

    def get_session_id(self, chat_id: int, repo: str) -> str | None:
        """Return existing Codex thread ID for this (chat, repo) pair, or None."""
        return self._sessions.get((chat_id, repo))

    def new_session(self, chat_id: int, repo: str) -> None:
        """Clear the session for this (chat, repo) pair so the next turn starts fresh."""
        self._sessions.pop((chat_id, repo), None)

    def has_running_proc(self, chat_id: int) -> bool:
        proc = self._running_procs.get(chat_id)
        if proc is not None and proc.returncode is None:
            return True
        return self._group_alive(chat_id)

    def _turn_in_flight(self, chat_id: int) -> bool:
        """True while run_turn is reading this chat's event stream."""
        return chat_id in self._abort_events

    def clear_pending_abort(self, chat_id: int) -> None:
        """Drop a stale abort flag so it cannot cancel a *future* turn.

        `_aborted_chats` only ever means "the turn in flight right now was asked
        to stop". A `/stop` that lands outside a turn — killing a process the
        previous turn detached from, or arriving in the window where the handler
        still holds the chat lock after run_turn returned — used to leave the
        flag set, and the next message was then swallowed before it ever reached
        Codex. Callers clear it when they begin a new turn.
        """
        self._aborted_chats.discard(chat_id)

    def _mark_aborted(self, chat_id: int, mark_pending: bool) -> None:
        """Arm the abort flag only when there is a turn for it to abort."""
        if mark_pending or self._turn_in_flight(chat_id):
            self._aborted_chats.add(chat_id)

    def _group_alive(self, chat_id: int) -> bool:
        """True if the process group of this chat's last turn still has members.

        Self-cleaning: forgets the group id as soon as it is gone.
        """
        pgid = self._pgids.get(chat_id)
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
        except OSError:
            self._pgids.pop(chat_id, None)
            return False
        return True

    async def _kill_orphan_group(self, chat_id: int) -> bool:
        """Kill descendants that outlived the process we spawned.

        `codex` is an npm shim: it can exit while the real binary keeps running
        in the same process group. Once the shim is reaped we have no Process
        handle left, so fall back to the group id recorded at spawn. Returns
        True if orphans were found and signalled.
        """
        if not self._group_alive(chat_id):
            return False
        pgid = self._pgids[chat_id]
        if pgid == os.getpgrp():  # never signal the bot's own group
            self._pgids.pop(chat_id, None)
            return False
        logger.warning("Codex left orphans in process group %d for chat %d; killing them", pgid, chat_id)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except OSError:
                break
            deadline = PROCESS_ABORT_TIMEOUT / _ORPHAN_POLL_INTERVAL
            for _ in range(int(deadline)):
                await asyncio.sleep(_ORPHAN_POLL_INTERVAL)
                if not self._group_alive(chat_id):
                    return True
        self._pgids.pop(chat_id, None)
        return True

    def _release_reader(self, chat_id: int) -> None:
        """Unblock run_turn's stdout reader, whether or not the process actually dies.

        The `codex` entry point is an npm shim that relays signals to the real
        binary; descendants inherit our stdout pipe, so a surviving child keeps
        it open forever. Never make the reader depend on EOF.
        """
        event = self._abort_events.get(chat_id)
        if event is not None:
            event.set()

    async def abort(self, chat_id: int, mark_pending: bool = False) -> bool:
        """Force-kill the Codex subprocess for a chat. Returns True if anything was stopped."""
        self._mark_aborted(chat_id, mark_pending)
        self._release_reader(chat_id)
        proc = self._running_procs.get(chat_id)
        if proc and proc.returncode is None:
            await self._terminate_proc(chat_id, proc)
            self._running_procs.pop(chat_id, None)
            await self._kill_orphan_group(chat_id)
            return True
        # The process we spawned is gone, but the npm shim may have left the real
        # binary running in its group — that is what "/stop says nothing running
        # while Codex keeps working" looked like.
        if await self._kill_orphan_group(chat_id):
            return True
        return mark_pending

    async def interrupt(self, chat_id: int, mark_pending: bool = False) -> Literal["idle", "cancelled", "forced"]:
        """Stop the active turn, asking politely first.

        SIGINT goes to the process group so it reaches the real Codex binary
        behind the npm shim. If the group is still alive after
        ``PROCESS_INTERRUPT_GRACE`` it is terminated and killed — leaving a
        SIGINT-ignoring process running only meant reporting a cancel that
        hadn't happened and requiring a follow-up `/stop`.

        Returns what actually happened so the caller can say so truthfully:
        ``idle`` (nothing to stop), ``cancelled`` (exited on SIGINT, or only a
        not-yet-started turn was flagged), ``forced`` (had to be killed).
        """
        self._mark_aborted(chat_id, mark_pending)
        self._release_reader(chat_id)
        proc = self._running_procs.get(chat_id)

        if not proc or proc.returncode is not None:
            if await self._kill_orphan_group(chat_id):
                return "forced"
            return "cancelled" if mark_pending else "idle"

        self._signal_proc_group(proc, signal.SIGINT)
        if await self._wait_for_proc_exit(proc, PROCESS_INTERRUPT_GRACE):
            self._running_procs.pop(chat_id, None)
            return "forced" if await self._kill_orphan_group(chat_id) else "cancelled"

        logger.warning("Codex process for chat %d ignored SIGINT; escalating to TERM/KILL", chat_id)
        await self._terminate_proc(chat_id, proc)
        self._running_procs.pop(chat_id, None)
        await self._kill_orphan_group(chat_id)
        return "forced"

    async def _terminate_proc(self, chat_id: int, proc: asyncio.subprocess.Process) -> None:
        self._signal_proc_group(proc, signal.SIGTERM)
        if not await self._wait_for_proc_exit(proc, PROCESS_ABORT_TIMEOUT):
            logger.warning("Codex process for chat %d did not stop after SIGTERM; sending SIGKILL", chat_id)
            self._signal_proc_group(proc, signal.SIGKILL)
            await self._wait_for_proc_exit(proc, PROCESS_ABORT_TIMEOUT)

    @staticmethod
    def _signal_proc_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sig)
            return
        except ProcessLookupError:
            return
        except Exception as e:
            logger.debug("Could not signal Codex process group for pid %s: %s", getattr(proc, "pid", "?"), e)

        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass

    @staticmethod
    async def _wait_for_proc_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    # ── Turn execution ────────────────────────────────────────────────

    async def run_turn(
        self,
        chat_id: int,
        repo: str,
        text: str,
        on_event,
        model: str | None = None,
    ) -> None:
        """Run one turn via `codex exec`, dispatching each JSON event to on_event.

        Spawns a fresh process per call, resuming the prior thread via
        `resume <thread_id>` when one is known, else starting a new thread.
        Captures the thread id from the `thread.started` event into
        self._sessions for the next call — the caller is responsible for
        persisting it to disk (mirrors ClaudeCodeManager's session handling).
        """
        assert self.cli_path is not None, "Codex CLI not found — install it or set CODEX_CLI_PATH"
        if chat_id in self._aborted_chats:
            self._aborted_chats.discard(chat_id)
            raise CodexTurnAborted()
        repo_dir = self.workspace_path(repo)
        session_key = (chat_id, repo)
        session_id = self._sessions.get(session_key)

        # A soft cancel leaves a SIGINT-ignoring process alive and detached. Kill it
        # now rather than run two Codex processes against the same workspace.
        stale = self._running_procs.pop(chat_id, None)
        if stale is not None and stale.returncode is None:
            logger.warning("Killing abandoned Codex process for chat %d before starting a new turn", chat_id)
            await self._terminate_proc(chat_id, stale)
        # Only ever set for a turn that ended abnormally — a clean turn forgets its
        # group, so background work it started on purpose survives into this one.
        await self._kill_orphan_group(chat_id)

        cmd = [self.cli_path, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            cmd.extend(["-m", model])
        if session_id:
            cmd.extend(["resume", session_id, "-"])
        else:
            cmd.append("-")

        logger.info("Codex: launching turn in %s (session=%s)", repo_dir, session_id or "new")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
            env=self._git_env(),
            start_new_session=True,
        )
        self._running_procs[chat_id] = proc
        # start_new_session makes the child a group leader, so pgid == pid. Kept
        # after the process is reaped so /stop can still reach orphaned children.
        self._pgids[chat_id] = proc.pid
        abort_event = asyncio.Event()
        self._abort_events[chat_id] = abort_event
        if chat_id in self._aborted_chats:
            await self._terminate_proc(chat_id, proc)
            self._running_procs.pop(chat_id, None)
            self._abort_events.pop(chat_id, None)
            if proc.stdin is not None:
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            self._aborted_chats.discard(chat_id)
            raise CodexTurnAborted()

        assert proc.stdin is not None
        try:
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.debug("Codex stdin closed before prompt write for chat %d: %s", chat_id, e)
        finally:
            proc.stdin.close()
            try:
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        stderr_lines: list[str] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                async for raw in proc.stderr:
                    line = raw.decode(errors="replace").rstrip()
                    if line:
                        stderr_lines.append(line)
                        logger.debug("Codex stderr (chat %d): %s", chat_id, line)
            except Exception as e:
                logger.debug("Codex stderr reader ended for chat %d: %s", chat_id, e)

        stderr_task = asyncio.create_task(_drain_stderr())
        abort_waiter = asyncio.create_task(abort_event.wait())

        try:
            assert proc.stdout is not None
            while True:
                read_task = asyncio.create_task(proc.stdout.readline())
                done, _pending = await asyncio.wait({read_task, abort_waiter}, return_when=asyncio.FIRST_COMPLETED)
                if read_task not in done:
                    read_task.cancel()
                    break
                raw_line = read_task.result()
                if not raw_line or chat_id in self._aborted_chats:
                    break
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Codex: non-JSON stdout line: %s", line)
                    continue

                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                    if thread_id:
                        self._sessions[session_key] = thread_id

                try:
                    await on_event(event)
                except Exception as e:
                    logger.error("Codex on_event handler failed for chat %d: %s", chat_id, e, exc_info=True)
        finally:
            abort_waiter.cancel()
            self._abort_events.pop(chat_id, None)
            # Bounded: after a soft cancel the process may still be alive, and an
            # unbounded wait here would hold the caller's per-chat lock forever.
            # On a clean EOF the exit is imminent and its code decides whether we
            # report a failure, so wait longer there than on the abort path.
            exit_timeout = PROCESS_ABORT_TIMEOUT if abort_event.is_set() else PROCESS_EXIT_WAIT
            if await self._wait_for_proc_exit(proc, exit_timeout):
                self._running_procs.pop(chat_id, None)
                if abort_event.is_set():
                    self._group_alive(chat_id)  # self-cleaning: forget the group once it has drained
                else:
                    # Ran to completion, so anything still in the group was started
                    # deliberately (a dev server, say) and is not ours to hunt down.
                    self._pgids.pop(chat_id, None)
            else:
                logger.warning("Codex process for chat %d outlived its turn; /stop will kill it", chat_id)
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if chat_id in self._aborted_chats:
            self._aborted_chats.discard(chat_id)
            raise CodexTurnAborted()

        if proc.returncode not in (0, None):
            stderr_text = "\n".join(stderr_lines[-20:])
            await on_event({"type": "_process_error", "returncode": proc.returncode, "stderr": stderr_text})


class CodexAppServerManager:
    """Persistent Codex app-server transport for Telegram chats.

    Each opted-in chat gets one JSONL-over-stdio app-server process. Thread ids
    are shared with ``CodexCodeManager`` so a chat can switch between app-server
    and ``codex exec resume`` without losing its conversation.
    """

    def __init__(self, owner: CodexCodeManager):
        self.owner = owner
        self._connections: dict[int, _AppServerConnection] = {}
        self._pending_interrupts: set[int] = set()

    def active(self, chat_id: int) -> bool:
        conn = self._connections.get(chat_id)
        return bool(conn and conn.proc.returncode is None and conn.reader_task and not conn.reader_task.done())

    def clear_pending_interrupt(self, chat_id: int) -> None:
        """Discard a cancellation left behind before an operation started.

        Callers do this synchronously at the boundary of a new user operation.
        A cancellation arriving during later preparation awaits is then retained
        and consumed by that operation, rather than leaking into the next one.
        """
        self._pending_interrupts.discard(chat_id)

    async def _start(self, chat_id: int) -> _AppServerConnection:
        if self.active(chat_id):
            return self._connections[chat_id]
        await self.stop(chat_id)
        assert self.owner.cli_path is not None, "Codex CLI not found"
        proc = await asyncio.create_subprocess_exec(
            self.owner.cli_path,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
            env=self.owner._git_env(),
            start_new_session=True,
        )
        conn = _AppServerConnection(proc=proc)
        self._connections[chat_id] = conn
        conn.reader_task = asyncio.create_task(self._read_forever(chat_id, conn))
        conn.stderr_task = asyncio.create_task(self._drain_stderr(chat_id, conn))
        try:
            await self._request(
                chat_id,
                conn,
                "initialize",
                {
                    "clientInfo": {
                        "name": "teleclaude",
                        "title": "Teleclaude Codex Bot",
                        "version": "prototype",
                    }
                },
            )
            await self._send(conn, {"method": "initialized", "params": {}})
        except Exception:
            await self.stop(chat_id)
            raise
        return conn

    async def _send(self, conn: _AppServerConnection, payload: dict) -> None:
        if conn.proc.stdin is None or conn.proc.stdin.is_closing():
            raise RuntimeError("Codex app-server stdin is closed")
        async with conn.write_lock:
            conn.proc.stdin.write((json.dumps(payload) + "\n").encode())
            await asyncio.wait_for(conn.proc.stdin.drain(), timeout=5)

    async def _request(
        self,
        chat_id: int,
        conn: _AppServerConnection,
        method: str,
        params: dict,
        timeout: float = 30,
    ) -> dict:
        request_id = conn.next_id
        conn.next_id += 1
        future = asyncio.get_running_loop().create_future()
        conn.pending[request_id] = future
        try:
            await self._send(conn, {"method": method, "id": request_id, "params": params})
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        finally:
            conn.pending.pop(request_id, None)

    async def _read_forever(self, chat_id: int, conn: _AppServerConnection) -> None:
        assert conn.proc.stdout is not None
        error: Exception = RuntimeError("Codex app-server exited")
        try:
            async for raw in conn.proc.stdout:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("Codex app-server emitted non-JSON: %s", raw.decode(errors="replace").rstrip())
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    future = conn.pending.get(request_id)
                    if future and not future.done():
                        if "error" in message:
                            detail = message["error"].get("message", str(message["error"]))
                            future.set_exception(RuntimeError(f"Codex app-server {detail}"))
                        else:
                            future.set_result(message.get("result", {}))
                    continue
                if request_id is not None and message.get("method"):
                    await self._send(
                        conn,
                        {
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": f"Unsupported server request: {message['method']}",
                            },
                        },
                    )
                    continue
                await self._notification(chat_id, conn, message)
        except asyncio.CancelledError:
            error = CodexTurnAborted()
            raise
        except Exception as exc:
            error = exc
            logger.error("Codex app-server reader failed for chat %d: %s", chat_id, exc, exc_info=True)
        finally:
            for future in list(conn.pending.values()):
                if not future.done():
                    future.set_exception(error)
            if conn.turn_done and not conn.turn_done.done():
                conn.turn_done.set_exception(error)
            if conn.compact_done and not conn.compact_done.done():
                conn.compact_done.set_exception(error)

    async def _drain_stderr(self, chat_id: int, conn: _AppServerConnection) -> None:
        assert conn.proc.stderr is not None
        try:
            async for raw in conn.proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    logger.debug("Codex app-server stderr (chat %d): %s", chat_id, line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Codex app-server stderr reader ended for chat %d: %s", chat_id, exc)

    @staticmethod
    def _normalize_item(item: dict) -> dict:
        normalized = dict(item)
        type_map = {
            "agentMessage": "agent_message",
            "commandExecution": "command_execution",
            "fileChange": "file_change",
            "mcpToolCall": "mcp_tool_call",
            "webSearch": "web_search",
        }
        item_type = item.get("type")
        normalized["type"] = type_map.get(item_type, item_type) if isinstance(item_type, str) else item_type
        if item.get("status") == "inProgress":
            normalized["status"] = "in_progress"
        if "exitCode" in item:
            normalized["exit_code"] = item["exitCode"]
        return normalized

    async def _notification(self, chat_id: int, conn: _AppServerConnection, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId")
        if thread_id and conn.active_thread_id and thread_id != conn.active_thread_id:
            return
        on_event = conn.on_event
        if method == "turn/started":
            turn = params.get("turn") or {}
            conn.active_turn_id = turn.get("id")
            cancellation_pending = chat_id in self._pending_interrupts
            conn.turn_ready = not cancellation_pending
            if cancellation_pending and conn.active_thread_id and conn.active_turn_id:
                self._pending_interrupts.discard(chat_id)
                asyncio.create_task(
                    self._interrupt_started_turn(chat_id, conn, conn.active_thread_id, conn.active_turn_id)
                )
            if on_event:
                await self._emit(chat_id, on_event, {"type": "turn.started"})
            return
        if method in {"item/started", "item/completed"} and on_event:
            event_type = "item.started" if method.endswith("started") else "item.completed"
            await self._emit(
                chat_id,
                on_event,
                {"type": event_type, "item": self._normalize_item(params.get("item") or {})},
            )
            return
        if method == "error" and on_event:
            error = params.get("error") or {}
            await self._emit(
                chat_id,
                on_event,
                {"type": "_process_error", "returncode": 1, "stderr": error.get("message", str(error))},
            )
            return
        if method == "thread/compacted":
            done = conn.compact_done
            conn.compact_done = None
            if done and not done.done():
                done.set_result(None)
            return
        if method != "turn/completed":
            return

        turn = params.get("turn") or {}
        status = turn.get("status")
        if on_event and status == "completed":
            await self._emit(chat_id, on_event, {"type": "turn.completed", "usage": {}})
        done = conn.turn_done
        conn.active_turn_id = None
        conn.turn_ready = False
        conn.on_event = None
        conn.turn_done = None
        if not done or done.done():
            return
        if status == "interrupted":
            done.set_exception(CodexTurnAborted())
        elif status == "failed":
            error = turn.get("error") or {}
            done.set_exception(RuntimeError(error.get("message", "Codex app-server turn failed")))
        elif status == "completed":
            done.set_result(None)
        else:
            done.set_exception(RuntimeError(f"Unexpected Codex app-server terminal status: {status!r}"))

    @staticmethod
    async def _emit(chat_id: int, on_event, event: dict) -> None:
        try:
            await on_event(event)
        except Exception as exc:
            logger.error("Codex app-server event handler failed for chat %d: %s", chat_id, exc, exc_info=True)

    async def _interrupt_started_turn(
        self,
        chat_id: int,
        conn: _AppServerConnection,
        thread_id: str,
        turn_id: str,
    ) -> None:
        try:
            await self._request(
                chat_id,
                conn,
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
            )
        except Exception as exc:
            logger.warning("Deferred Codex app-server interrupt failed for chat %d: %s", chat_id, exc)
            await self.stop(chat_id)

    async def _load_thread(
        self,
        chat_id: int,
        conn: _AppServerConnection,
        repo: str,
        cwd: Path,
        model: str | None,
    ) -> str:
        if conn.active_repo == repo and conn.active_thread_id:
            return conn.active_thread_id
        session_key = (chat_id, repo)
        thread_id = self.owner._sessions.get(session_key)
        common: dict[str, Any] = {
            "cwd": str(cwd),
            "approvalPolicy": "never",
            # The thread request enum uses kebab-case on the wire. This is the
            # opposite of turn/start's nested SandboxPolicy discriminator.
            "sandbox": "danger-full-access",
        }
        if model:
            common["model"] = model
        try:
            if thread_id:
                result = await self._request(chat_id, conn, "thread/resume", {"threadId": thread_id, **common})
            else:
                common["serviceName"] = "teleclaude"
                result = await self._request(chat_id, conn, "thread/start", common)
        except Exception:
            # A timed-out mutating request may still have succeeded server-side.
            # Recycle the connection so the next operation cannot collide with
            # protocol state whose outcome this client no longer knows.
            await self.stop(chat_id)
            raise
        loaded = (result.get("thread") or {}).get("id")
        if not loaded:
            raise RuntimeError("Codex app-server did not return a thread id")
        self.owner._sessions[session_key] = loaded
        conn.active_repo = repo
        conn.active_thread_id = loaded
        return loaded

    async def run_turn(self, chat_id: int, repo: str, text: str, on_event, model: str | None = None) -> None:
        if chat_id in self._pending_interrupts:
            self._pending_interrupts.discard(chat_id)
            raise CodexTurnAborted()
        conn = await self._start(chat_id)
        cwd = self.owner.workspace_path(repo)
        thread_id = await self._load_thread(chat_id, conn, repo, cwd, model)
        if chat_id in self._pending_interrupts:
            self._pending_interrupts.discard(chat_id)
            raise CodexTurnAborted()
        done = asyncio.get_running_loop().create_future()
        conn.turn_done = done
        conn.on_event = on_event
        conn.turn_ready = False
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "cwd": str(cwd),
            "approvalPolicy": "never",
            # The nested turn SandboxPolicy discriminator uses camelCase on the
            # wire. This is the opposite of ThreadStartParams.sandbox above.
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }
        if model:
            params["model"] = model
        try:
            result = await self._request(chat_id, conn, "turn/start", params)
            if not done.done():
                conn.active_turn_id = (result.get("turn") or {}).get("id") or conn.active_turn_id
            if chat_id in self._pending_interrupts:
                self._pending_interrupts.discard(chat_id)
                if conn.active_turn_id:
                    await self._request(
                        chat_id,
                        conn,
                        "turn/interrupt",
                        {"threadId": thread_id, "turnId": conn.active_turn_id},
                    )
                else:
                    raise CodexTurnAborted()
            await done
        except Exception:
            uncertain_server_state = conn.turn_done is done
            if uncertain_server_state:
                conn.turn_done = None
                conn.on_event = None
                conn.active_turn_id = None
                conn.turn_ready = False
                await self.stop(chat_id)
            raise

    async def steer(self, chat_id: int, text: str) -> bool:
        """Add user input to the active turn, returning False if none exists."""
        conn = self._connections.get(chat_id)
        if (
            not conn
            or not conn.active_thread_id
            or not conn.active_turn_id
            or not conn.turn_ready
            or not conn.turn_done
            or conn.turn_done.done()
        ):
            return False
        expected_turn_id = conn.active_turn_id
        result = await self._request(
            chat_id,
            conn,
            "turn/steer",
            {
                "threadId": conn.active_thread_id,
                "expectedTurnId": expected_turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        returned_turn_id = result.get("turnId")
        if returned_turn_id != expected_turn_id:
            raise RuntimeError(
                f"Codex app-server steered unexpected turn {returned_turn_id!r}; expected {expected_turn_id!r}"
            )
        return True

    async def execute_slash(
        self,
        chat_id: int,
        repo: str,
        command: str,
        model: str | None = None,
    ) -> str:
        """Execute a supported TUI-style command through app-server methods.

        Slash commands are normally parsed by the interactive Codex TUI, not
        by app-server. Teleclaude exposes selected app-server operations as
        ordinary Telegram commands; flexible ``//`` input bypasses this method.
        """
        name, _, args = command.strip().partition(" ")
        name = name.lower().lstrip("/")
        supported = {"compact", "goal", "status", "usage"}
        if name not in supported:
            return (
                f"Unsupported Codex stream command: /{name}\n"
                "Supported: /status, /usage, /compact, /goal [objective|resume|clear]"
            )
        if name == "compact" and chat_id in self._pending_interrupts:
            self._pending_interrupts.discard(chat_id)
            raise CodexTurnAborted()

        conn = await self._start(chat_id)
        cwd = self.owner.workspace_path(repo)
        thread_id = await self._load_thread(chat_id, conn, repo, cwd, model)

        if name == "compact" and chat_id in self._pending_interrupts:
            self._pending_interrupts.discard(chat_id)
            raise CodexTurnAborted()

        if name == "compact":
            if args:
                return "Usage: /compact"
            done = asyncio.get_running_loop().create_future()
            conn.compact_done = done
            try:
                await self._request(chat_id, conn, "thread/compact/start", {"threadId": thread_id})
                await done
            except Exception:
                if conn.compact_done is done:
                    conn.compact_done = None
                    await self.stop(chat_id)
                raise
            finally:
                if conn.compact_done is done:
                    conn.compact_done = None
            return "Codex context compaction completed."

        if name == "status":
            if args:
                return "Usage: /status"
            result = await self._request(
                chat_id,
                conn,
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
            thread = result.get("thread") or {}
            runtime_status = thread.get("status") or "loaded"
            active_model = thread.get("model") or model or "CLI default"
            return (
                "Codex app-server status\n"
                f"Thread: {thread_id}\n"
                f"Repository: {repo}\n"
                f"Model: {active_model}\n"
                f"Runtime: {runtime_status}"
            )

        if name == "goal":
            goal_args = args.strip()
            if goal_args.lower() == "clear":
                result = await self._request(
                    chat_id,
                    conn,
                    "thread/goal/clear",
                    {"threadId": thread_id},
                )
                return "Codex goal cleared." if result.get("cleared") else "No Codex goal was set."
            if goal_args.lower() == "resume":
                # Codex stalls a goal by moving it out of "active" (blocked,
                # paused, or a usage/budget limit). Resuming is a status flip
                # back to active; the next turn picks the objective back up.
                current = await self._request(chat_id, conn, "thread/goal/get", {"threadId": thread_id})
                if not current.get("goal"):
                    return "No Codex goal is set.\nSet one with /goal <objective>."
                result = await self._request(
                    chat_id,
                    conn,
                    "thread/goal/set",
                    {"threadId": thread_id, "status": "active"},
                )
                resumed = True
            elif goal_args:
                result = await self._request(
                    chat_id,
                    conn,
                    "thread/goal/set",
                    {"threadId": thread_id, "objective": goal_args},
                )
                resumed = False
            else:
                result = await self._request(
                    chat_id,
                    conn,
                    "thread/goal/get",
                    {"threadId": thread_id},
                )
                resumed = False
            goal = result.get("goal")
            if not goal:
                return "No Codex goal is set.\nSet one with /goal <objective>."
            budget = goal.get("tokenBudget")
            budget_line = f"\nToken budget: {budget}" if budget is not None else ""
            status = goal.get("status", "unknown")
            if resumed:
                hint = "\nSend a message to continue the goal."
            elif status in {"paused", "blocked", "usageLimited", "budgetLimited"}:
                hint = "\nResume it with /goal resume."
            else:
                hint = ""
            return (
                "Codex goal\n"
                f"Objective: {goal.get('objective', '')}\n"
                f"Status: {status}\n"
                f"Tokens used: {goal.get('tokensUsed', 0)}{budget_line}{hint}"
            )

        if name == "usage":
            if args:
                return "Usage filters are not supported yet. Use /usage without arguments."
            result = await self._request(chat_id, conn, "account/usage/read", {})
            rendered = json.dumps(result, indent=2, sort_keys=True)
            if len(rendered) > 3500:
                rendered = rendered[:3500] + "\n…"
            return f"Codex account usage\n{rendered}"

        raise AssertionError(f"unhandled supported command: {name}")

    async def interrupt(self, chat_id: int, mark_pending: bool = False) -> Literal["idle", "cancelled", "forced"]:
        conn = self._connections.get(chat_id)
        if conn and conn.compact_done and not conn.compact_done.done():
            await self.stop(chat_id)
            return "forced"
        if not conn or not conn.active_thread_id or not conn.active_turn_id:
            if mark_pending or (conn and conn.turn_done and not conn.turn_done.done()):
                self._pending_interrupts.add(chat_id)
                return "cancelled"
            return "idle"
        # Stop accepting follow-up input before awaiting the protocol response;
        # /cancel and a simultaneous message are processed concurrently.
        conn.turn_ready = False
        try:
            await self._request(
                chat_id,
                conn,
                "turn/interrupt",
                {"threadId": conn.active_thread_id, "turnId": conn.active_turn_id},
            )
            return "cancelled"
        except Exception as exc:
            logger.warning("Codex app-server interrupt failed for chat %d: %s", chat_id, exc)
            await self.stop(chat_id)
            return "forced"

    async def stop(self, chat_id: int, mark_pending: bool = False) -> bool:
        if mark_pending:
            self._pending_interrupts.add(chat_id)
        conn = self._connections.pop(chat_id, None)
        if not conn:
            return mark_pending
        if conn.reader_task and not conn.reader_task.done():
            conn.reader_task.cancel()
        if conn.stderr_task and not conn.stderr_task.done():
            conn.stderr_task.cancel()
        if conn.proc.returncode is None:
            CodexCodeManager._signal_proc_group(conn.proc, signal.SIGTERM)
            if not await CodexCodeManager._wait_for_proc_exit(conn.proc, PROCESS_ABORT_TIMEOUT):
                CodexCodeManager._signal_proc_group(conn.proc, signal.SIGKILL)
                await CodexCodeManager._wait_for_proc_exit(conn.proc, PROCESS_ABORT_TIMEOUT)
        for task in (conn.reader_task, conn.stderr_task):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        return True

    async def stop_all(self) -> None:
        for chat_id in list(self._connections):
            await self.stop(chat_id)
