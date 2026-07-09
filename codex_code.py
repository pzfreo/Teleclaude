"""Codex CLI integration — routes agent-mode messages through `codex` CLI.

Prototype counterpart to claude_code.py. Unlike Claude Code CLI, `codex exec`
is single-shot: each turn spawns a fresh subprocess and exits, and there is no
equivalent to Claude Code's `--input-format stream-json` for feeding follow-up
messages into a live process. Conversation continuity across turns is via
`codex exec resume <thread_id> <prompt>` instead. This means there is no
persistent "stream mode" here — no background process to push scheduled or
monitor-triggered messages into.

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
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 120  # 2 minutes for git operations
NPM_UPDATE_TIMEOUT = 180  # 3 minutes for npm update
PROCESS_ABORT_TIMEOUT = 3  # seconds to wait after TERM before escalating


class CodexTurnAborted(Exception):
    """Raised when a Codex turn is intentionally stopped by the user."""


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
        return proc is not None and proc.returncode is None

    async def abort(self, chat_id: int, mark_pending: bool = False) -> bool:
        """Kill the in-flight Codex subprocess for a chat. Returns True if one was killed."""
        proc = self._running_procs.get(chat_id)
        if not proc or proc.returncode is not None:
            if mark_pending:
                self._aborted_chats.add(chat_id)
                return True
            return False
        self._aborted_chats.add(chat_id)
        await self._terminate_proc(chat_id, proc)
        self._running_procs.pop(chat_id, None)
        return True

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
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
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
        if chat_id in self._aborted_chats:
            await self._terminate_proc(chat_id, proc)
            self._running_procs.pop(chat_id, None)
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

        try:
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                if chat_id in self._aborted_chats:
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
            await proc.wait()
            self._running_procs.pop(chat_id, None)
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if chat_id in self._aborted_chats:
            self._aborted_chats.discard(chat_id)
            raise CodexTurnAborted()

        if proc.returncode != 0:
            stderr_text = "\n".join(stderr_lines[-20:])
            await on_event({"type": "_process_error", "returncode": proc.returncode, "stderr": stderr_text})
