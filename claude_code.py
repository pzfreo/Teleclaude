"""Claude Code CLI integration — routes agent-mode messages through `claude` CLI."""

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class ClaudeCodeManager:
    """Manages local clones and Claude Code CLI sessions."""

    def __init__(self, github_token: str, workspace_root: str | None = None, cli_path: str | None = None):
        self.github_token = github_token
        self.workspace_root = Path(workspace_root or os.getenv("CLAUDE_CODE_WORKSPACE") or "workspaces")
        self.cli_path = cli_path or os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude")
        self._sessions: dict[int, str] = {}  # chat_id → session_id

    @property
    def available(self) -> bool:
        return bool(self.cli_path)

    # ── Workspace management ─────────────────────────────────────────

    def workspace_path(self, repo: str) -> Path:
        """Return local path for a repo clone: workspaces/{owner}/{name}/"""
        owner, name = repo.split("/", 1)
        return self.workspace_root / owner / name

    async def ensure_clone(self, repo: str) -> Path:
        """Clone the repo if it doesn't already exist locally. Returns the path."""
        path = self.workspace_path(repo)
        if (path / ".git").is_dir():
            logger.info("Workspace already exists: %s", path)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://{self.github_token}@github.com/{repo}.git"
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
        """Run a git command as an async subprocess."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
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

    # ── CLI invocation ───────────────────────────────────────────────

    async def run(
        self,
        chat_id: int,
        repo: str,
        prompt: str,
        branch: str | None = None,
        model: str | None = None,
        on_progress=None,
    ) -> str:
        """Run a prompt through Claude Code CLI and return the result.

        Args:
            chat_id: Telegram chat ID (for session tracking)
            repo: GitHub repo in owner/name format
            prompt: The user's message
            branch: Optional branch to work on
            model: Claude model ID
            on_progress: async callback(tool_name: str) for progress updates

        Returns:
            The final text result from Claude Code
        """
        repo_dir = self.workspace_path(repo)
        if not (repo_dir / ".git").is_dir():
            await self.ensure_clone(repo)

        # Checkout branch if specified
        if branch:
            try:
                await self.checkout_branch(repo, branch)
            except RuntimeError as e:
                logger.warning("Branch checkout failed: %s", e)

        # Pull latest
        await self.pull_latest(repo)

        # Build CLI command
        assert self.cli_path is not None, "Claude CLI not found — install it or set CLAUDE_CLI_PATH"
        cmd = [self.cli_path, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]

        if model:
            cmd.extend(["--model", model])

        # Session handling: resume existing or start new
        session_id = self._sessions.get(chat_id)
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            new_id = str(uuid.uuid4())
            cmd.extend(["--session-id", new_id])

        cmd.extend(
            [
                "--append-system-prompt",
                "Responding via Telegram. Keep responses concise for mobile reading.",
            ]
        )

        cmd.append(prompt)

        logger.info("Claude Code: running in %s (session=%s)", repo_dir, session_id or "new")

        # Launch subprocess (large limit: CLI emits big JSON lines for tool results)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,  # 10 MB line buffer
        )

        result_text = ""
        returned_session_id = None

        try:
            result_text, returned_session_id = await asyncio.wait_for(
                self._read_stream(proc, on_progress),
                timeout=600,  # 10 minutes
            )
        except TimeoutError:
            logger.warning("Claude Code timed out after 10 minutes")
            proc.kill()
            await proc.wait()
            result_text = "(Claude Code timed out after 10 minutes)"
        except Exception as e:
            logger.error("Claude Code stream error: %s", e, exc_info=True)
            proc.kill()
            await proc.wait()
            result_text = f"(Claude Code error: {e})"

        # Store session ID for continuity
        if returned_session_id:
            self._sessions[chat_id] = returned_session_id
        elif not session_id:
            # If we started a new session but didn't get one back,
            # store the one we generated so --resume works next time
            self._sessions[chat_id] = new_id

        return result_text

    async def _read_stream(self, proc, on_progress) -> tuple[str, str | None]:
        """Read stream-json output from Claude Code.

        Returns (result_text, session_id).
        """
        result_text = ""
        session_id = None

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
                # Report tool_use blocks with context for progress display
                message = event.get("message", {})
                content = message.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            if on_progress:
                                try:
                                    await on_progress(block)
                                except Exception:
                                    pass

            elif event_type == "result":
                result_text = event.get("result", "")
                session_id = event.get("session_id")

        # Wait for process to finish
        await proc.wait()

        if proc.returncode != 0 and not result_text:
            stderr = await proc.stderr.read()
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            result_text = f"(Claude Code exited with code {proc.returncode}: {err_msg})"

        return result_text, session_id
