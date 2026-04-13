"""Claude Code CLI integration — routes agent-mode messages through `claude` CLI."""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 120  # 2 minutes for git operations
NPM_UPDATE_TIMEOUT = 180  # 3 minutes for npm update


def _get_cli_timeout() -> int:
    """Get CLI timeout in seconds from CLAUDE_CLI_TIMEOUT env var (default 600, min 60)."""
    raw = os.getenv("CLAUDE_CLI_TIMEOUT", "600")
    try:
        return max(60, int(raw))
    except ValueError:
        return 600


async def update_claude_cli() -> tuple[bool, str]:
    """Update Claude CLI to latest version via npm.

    Returns:
        (success: bool, message: str) - success flag and status message
    """
    npm_path = shutil.which("npm")
    if not npm_path:
        return False, "npm not found"

    logger.info("Checking for Claude CLI updates...")

    try:
        # Run npm update -g @anthropic-ai/claude-code
        proc = await asyncio.create_subprocess_exec(
            npm_path,
            "update",
            "-g",
            "@anthropic-ai/claude-code",
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
            # EACCES means permission denied — might be already latest or need sudo
            if "EACCES" in err_msg or "permission denied" in err_msg.lower():
                logger.warning("npm update failed (permissions): %s", err_msg)
                return False, "Permission denied (may already be latest)"
            logger.warning("npm update failed: %s", err_msg)
            return False, f"Update failed: {err_msg[:100]}"

        # Get current version
        version = await get_claude_cli_version()
        logger.info("Claude CLI updated successfully to %s", version or "unknown")
        return True, f"Updated to {version}" if version else "Updated successfully"

    except Exception as e:
        logger.error("Failed to update Claude CLI: %s", e)
        return False, str(e)


async def get_claude_cli_version() -> str | None:
    """Get the installed Claude CLI version.

    Returns:
        Version string (e.g., "0.5.2") or None if unavailable
    """
    cli_path = shutil.which("claude")
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
            # Output is typically "@anthropic-ai/claude-code/0.5.2" or just "0.5.2"
            if "/" in version:
                version = version.split("/")[-1]
            return version

    except Exception as e:
        logger.warning("Failed to get Claude CLI version: %s", e)

    return None


class ClaudeCodeManager:
    """Manages local clones and Claude Code CLI sessions."""

    def __init__(self, github_token: str, workspace_root: str | None = None, cli_path: str | None = None):
        self.github_token = github_token
        self.workspace_root = Path(workspace_root or os.getenv("CLAUDE_CODE_WORKSPACE") or "workspaces")
        self.cli_path = cli_path or os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude")
        self._sessions: dict[int, str] = {}  # chat_id → session_id
        self._running_procs: dict[int, asyncio.subprocess.Process] = {}  # chat_id → active proc
        self._proc_stdins: dict[int, asyncio.StreamWriter] = {}  # chat_id → stdin writer
        self._proc_repos: dict[int, str] = {}  # chat_id → repo the process was launched for
        self._is_processing: dict[int, bool] = {}  # chat_id → True while awaiting a result

    @property
    def available(self) -> bool:
        return bool(self.cli_path)

    # ── Workspace management ─────────────────────────────────────────

    def workspace_path(self, repo: str) -> Path:
        """Return local path for a repo clone: workspaces/{owner}/{name}/"""
        owner, name = repo.split("/", 1)
        path = (self.workspace_root / owner / name).resolve()
        # Prevent path traversal outside the workspace root
        root = self.workspace_root.resolve()
        if not str(path).startswith(str(root) + os.sep) and path != root:
            raise ValueError(f"Path traversal blocked: {repo!r} resolves outside workspace root")
        return path

    def _git_env(self) -> dict[str, str]:
        """Build environment for git subprocesses with credential helper.

        Uses GIT_ASKPASS with a helper script that returns the GitHub token,
        avoiding token exposure in URLs, process lists, shell history, or logs.
        """
        env = os.environ.copy()
        if self.github_token:
            # GIT_ASKPASS is called by git for username/password.
            # printf outputs the token for any prompt.
            env["GIT_ASKPASS"] = "/bin/sh"
            env["GIT_TERMINAL_PROMPT"] = "0"
            # Use a credential helper that feeds user=token, password=token
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
            logger.info("Workspace already exists: %s", path)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{repo}.git"
        await self._git(path.parent, "clone", url, path.name)
        logger.info("Cloned %s to %s", repo, path)
        return path

    async def checkout_branch(self, repo: str, branch: str) -> str:
        """Fetch and checkout a branch in the local clone."""
        cwd = self.workspace_path(repo)
        if not (cwd / ".git").is_dir():
            await self.ensure_clone(repo)
        await self._git(cwd, "fetch", "origin")
        # Try checking out — if it's a remote branch not yet local, track it
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
            # Non-fast-forward is fine — local changes may exist from Claude Code
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

    # ── Session management ───────────────────────────────────────────

    def get_session_id(self, chat_id: int) -> str | None:
        """Return existing session ID for a chat, or None."""
        return self._sessions.get(chat_id)

    def new_session(self, chat_id: int) -> None:
        """Clear the session so the next message starts fresh."""
        self._sessions.pop(chat_id, None)
        self._proc_repos.pop(chat_id, None)

    async def abort(self, chat_id: int) -> bool:
        """Kill the running CLI subprocess for a chat. Returns True if a process was killed."""
        had_proc = chat_id in self._running_procs and self.has_running_proc(chat_id)
        await self._kill_proc(chat_id)
        self._is_processing.pop(chat_id, None)
        return had_proc

    def has_running_proc(self, chat_id: int) -> bool:
        """Check if a CLI subprocess is currently running for a chat."""
        proc = self._running_procs.get(chat_id)
        return proc is not None and proc.returncode is None

    def is_processing(self, chat_id: int) -> bool:
        """Check if a CLI turn is actively being processed (mid-turn, not idle between turns)."""
        return self._is_processing.get(chat_id, False)

    async def send_followup(self, chat_id: int, text: str) -> bool:
        """Send a follow-up message to a running CLI process via stdin.

        Returns True if the message was sent, False if no process is running.
        """
        stdin = self._proc_stdins.get(chat_id)
        if not stdin or stdin.is_closing():
            return False
        if not self.has_running_proc(chat_id):
            self._proc_stdins.pop(chat_id, None)
            return False

        msg = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        try:
            stdin.write((msg + "\n").encode("utf-8"))
            await stdin.drain()
            logger.info("Sent follow-up to chat %d: %s", chat_id, text[:80])
            return True
        except Exception as e:
            logger.warning("Failed to send follow-up to chat %d: %s", chat_id, e)
            self._proc_stdins.pop(chat_id, None)
            return False

    # ── CLI invocation ───────────────────────────────────────────────

    async def _kill_proc(self, chat_id: int) -> None:
        """Kill an existing CLI process for a chat and clean up."""
        self._proc_stdins.pop(chat_id, None)
        proc = self._running_procs.pop(chat_id, None)
        self._proc_repos.pop(chat_id, None)
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()

    async def _ensure_proc(
        self,
        chat_id: int,
        repo: str,
        model: str | None,
        permission_mode: str | None,
    ) -> asyncio.subprocess.Process:
        """Return a running CLI process for this chat, launching one if needed.

        Reuses the existing process when the repo hasn't changed. Kills and
        restarts if the repo changed or the process exited.
        """
        existing = self._running_procs.get(chat_id)
        same_repo = self._proc_repos.get(chat_id) == repo

        if existing and existing.returncode is None and same_repo:
            return existing

        # Different repo or dead process — start fresh
        await self._kill_proc(chat_id)

        repo_dir = self.workspace_path(repo)
        assert self.cli_path is not None, "Claude CLI not found — install it or set CLAUDE_CLI_PATH"
        cmd = [
            self.cli_path,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]

        if model:
            cmd.extend(["--model", model])

        if permission_mode:
            cmd.extend(["--permission-mode", permission_mode])

        session_id = self._sessions.get(chat_id)
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            new_id = str(uuid.uuid4())
            cmd.extend(["--session-id", new_id])
            self._sessions[chat_id] = new_id

        cmd.extend(
            [
                "--append-system-prompt",
                "You are responding to a user via Telegram chat, NOT a terminal. Important constraints:\n"
                "- The user can only see your final text responses — not tool calls, file diffs, or intermediate output.\n"
                "- Do NOT ask the user to run CLI commands or use terminal features.\n"
                "- When presenting plans, include the full plan text in your response so the user can read it.\n"
                "- Keep responses concise for mobile reading.\n"
                "- If you need user input, ask a clear question in your response text.",
            ]
        )

        logger.info("Claude Code: launching process in %s (session=%s)", repo_dir, session_id or "new")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
        )

        self._running_procs[chat_id] = proc
        self._proc_stdins[chat_id] = proc.stdin
        self._proc_repos[chat_id] = repo
        return proc

    async def run(
        self,
        chat_id: int,
        repo: str,
        prompt: str,
        branch: str | None = None,
        model: str | None = None,
        on_progress=None,
        on_timeout=None,
        permission_mode: str | None = None,
    ) -> str:
        """Run a prompt through Claude Code CLI and return the result.

        Reuses an existing CLI process when possible (same repo, process alive).
        The process stays alive between turns for fast follow-ups via stdin.

        Args:
            chat_id: Telegram chat ID (for session tracking)
            repo: GitHub repo in owner/name format
            prompt: The user's message
            branch: Optional branch to work on
            model: Claude model ID
            on_progress: async callback(block: dict) for progress updates
            on_timeout: async callback(elapsed_seconds: int) -> bool; return True to continue
            permission_mode: CLI permission mode override (e.g. "plan")

        Returns:
            The final text result from Claude Code
        """
        repo_dir = self.workspace_path(repo)
        if not (repo_dir / ".git").is_dir():
            await self.ensure_clone(repo)

        if branch:
            try:
                await self.checkout_branch(repo, branch)
            except RuntimeError as e:
                logger.warning("Branch checkout failed: %s", e)

        await self.pull_latest(repo)

        proc = await self._ensure_proc(chat_id, repo, model, permission_mode)

        # Send prompt via stdin
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
        assert proc.stdin is not None, "stdin pipe not available"
        proc.stdin.write((msg + "\n").encode("utf-8"))
        await proc.stdin.drain()

        self._is_processing[chat_id] = True
        result_text = ""
        returned_session_id = None
        cli_timeout = _get_cli_timeout()

        try:
            start_time = time.monotonic()
            stream_task = asyncio.create_task(self._read_stream(proc, on_progress))

            while True:
                try:
                    result_text, returned_session_id = await asyncio.wait_for(
                        asyncio.shield(stream_task), timeout=cli_timeout
                    )
                    break
                except TimeoutError:
                    if stream_task.done():
                        result_text, returned_session_id = stream_task.result()
                        break

                    elapsed = int(time.monotonic() - start_time)

                    should_continue = False
                    if on_timeout:
                        try:
                            should_continue = await on_timeout(elapsed)
                        except Exception as exc:
                            logger.warning("Timeout callback failed: %s", exc)

                    if should_continue:
                        logger.info("CLI timeout extended for chat %d (elapsed %ds)", chat_id, elapsed)
                        continue

                    logger.warning("Claude Code CLI timed out after %ds for chat %d", elapsed, chat_id)
                    await self._kill_proc(chat_id)
                    result_text = f"(Claude Code timed out after {elapsed // 60} minutes)"
                    break

        except asyncio.CancelledError:
            await self._kill_proc(chat_id)
            return "(aborted)"
        except Exception as e:
            logger.error("Claude Code stream error: %s", e, exc_info=True)
            await self._kill_proc(chat_id)
            result_text = f"(Claude Code error: {e})"
        finally:
            self._is_processing[chat_id] = False

        if returned_session_id:
            self._sessions[chat_id] = returned_session_id

        return result_text

    async def _read_stream(self, proc, on_progress) -> tuple[str, str | None]:
        """Read stream-json output until the result event (one turn).

        The process stays alive after this returns — it's waiting for the next
        stdin message. We break out of the read loop on the result event rather
        than waiting for stdout EOF / process exit.

        Returns (result_text, session_id).
        """
        result_text = ""
        session_id = None
        last_assistant_text = ""
        text_was_streamed = False
        pending_system_events: list[dict] = []
        result_stats: dict = {}
        got_result = False

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            if event_type == "assistant":
                message = event.get("message", {})
                content = message.get("content", [])
                if isinstance(content, list):
                    texts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                            if on_progress:
                                try:
                                    await on_progress(block)
                                    text_was_streamed = True
                                except Exception:
                                    pass
                        elif block.get("type") == "tool_use":
                            if on_progress:
                                try:
                                    await on_progress(block)
                                except Exception:
                                    pass
                    if texts:
                        last_assistant_text = "\n".join(t for t in texts if t)

            elif event_type == "result":
                result_text = event.get("result", "")
                session_id = event.get("session_id")
                for key in ("cost_usd", "num_turns", "usage"):
                    if key in event:
                        result_stats[key] = event[key]
                got_result = True
                break  # Turn complete — process stays alive for next turn

            elif event_type == "system":
                subtype = event.get("subtype", "")
                if subtype:
                    pending_system_events.append({"_type": "system_event", "subtype": subtype})

        # If stdout closed without a result event, the process died
        if not got_result:
            await proc.wait()
            if proc.returncode != 0 and not result_text:
                if proc.returncode == -9:
                    result_text = "(stopped)"
                else:
                    stderr = await proc.stderr.read()
                    err_msg = stderr.decode("utf-8", errors="replace").strip()
                    result_text = f"(Claude Code exited with code {proc.returncode}: {err_msg})"

        if text_was_streamed:
            result_text = ""
        elif last_assistant_text and len(last_assistant_text) > len(result_text):
            result_text = last_assistant_text

        if on_progress:
            for sys_event in pending_system_events:
                try:
                    await on_progress(sys_event)
                except Exception:
                    pass
            if result_stats:
                try:
                    await on_progress({"_type": "stats", **result_stats})
                except Exception:
                    pass

        return result_text, session_id
