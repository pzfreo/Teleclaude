"""Claude Code CLI integration — routes agent-mode messages through `claude` CLI."""

import asyncio
import json
import logging
import os
import shlex
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 120  # 2 minutes for git operations
# Global MCP config merged into every CLI invocation alongside any per-repo .mcp.json
GLOBAL_MCP_CONFIG = Path(__file__).parent / "mcp_global.json"
NPM_UPDATE_TIMEOUT = 180  # 3 minutes for npm update


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


def _resolve_mcp_config(mcp_config_path: Path) -> dict:
    """Load .mcp.json and work around Claude Code ignoring the cwd field.

    Claude Code CLI ignores the 'cwd' field in server configs passed via
    --mcp-config, running the server process in the CLI's own working directory
    instead. We work around this by wrapping the command with bash -c 'cd {cwd} && ...'
    so the server starts in the correct directory regardless.
    """
    with open(mcp_config_path) as f:
        config = json.load(f)

    for server_config in config.get("mcpServers", {}).values():
        cwd = server_config.pop("cwd", None)
        if not cwd:
            continue
        cmd = server_config["command"]
        args = server_config.get("args", [])
        arg_str = " ".join(shlex.quote(str(a)) for a in args)
        server_config["command"] = "bash"
        server_config["args"] = ["-c", f"cd {shlex.quote(cwd)} && {shlex.quote(cmd)} {arg_str}"]

    return config


def _build_mcp_config(repo_mcp: Path) -> str | None:
    """Merge global mcp_global.json with an optional per-repo .mcp.json.

    Returns the merged config as a JSON string for --mcp-config, or None if
    neither file exists.
    """
    merged: dict = {"mcpServers": {}}

    if GLOBAL_MCP_CONFIG.is_file():
        try:
            with open(GLOBAL_MCP_CONFIG) as f:
                global_cfg = json.load(f)
            merged["mcpServers"].update(global_cfg.get("mcpServers", {}))
            logger.info("Claude Code: loaded global MCP config from %s", GLOBAL_MCP_CONFIG)
        except Exception as e:
            logger.warning("Failed to load global MCP config: %s", e)

    if repo_mcp.is_file():
        try:
            repo_cfg = _resolve_mcp_config(repo_mcp)
            for name, srv in repo_cfg.get("mcpServers", {}).items():
                merged["mcpServers"][name] = srv
            logger.info("Claude Code: loaded repo MCP config from %s", repo_mcp)
        except Exception as e:
            logger.warning("Failed to load repo MCP config %s: %s", repo_mcp, e)

    if not merged["mcpServers"]:
        return None
    return json.dumps(merged)


class ClaudeCodeManager:
    """Manages local clones and Claude Code CLI sessions."""

    def __init__(self, github_token: str, workspace_root: str | None = None, cli_path: str | None = None):
        self.github_token = github_token
        self.workspace_root = Path(workspace_root or os.getenv("CLAUDE_CODE_WORKSPACE") or "workspaces")
        self.cli_path = cli_path or os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude")
        self._sessions: dict[tuple[int, str], str] = {}  # (chat_id, repo) → session_id
        self._running_procs: dict[int, asyncio.subprocess.Process] = {}  # chat_id → active proc
        self._proc_stdins: dict[int, asyncio.StreamWriter] = {}  # chat_id → stdin writer
        self._proc_repos: dict[int, str] = {}  # chat_id → repo the process was launched for
        self._stdin_locks: dict[int, asyncio.Lock] = {}  # chat_id → lock for stdin writes
        self._last_models: dict[int, str] = {}  # chat_id → resolved model id from most recent CLI init
        self._stream_tasks: dict[int, asyncio.Task] = {}  # chat_id → continuous reader task (stream mode)
        self._stderr_tasks: dict[int, asyncio.Task] = {}  # chat_id → background stderr-to-log task
        self._control_request_counter: dict[int, int] = {}  # chat_id → monotonic request counter

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

    def get_session_id(self, chat_id: int, repo: str) -> str | None:
        """Return existing session ID for this (chat, repo) pair, or None."""
        return self._sessions.get((chat_id, repo))

    @staticmethod
    def _session_jsonl_path(session_id: str, repo_dir: Path) -> Path:
        """Path where the CLI stores a session's transcript.

        The CLI encodes the cwd by replacing path separators with hyphens,
        e.g. /app/workspaces/pzfreo/Teleclaude → -app-workspaces-pzfreo-Teleclaude.
        """
        encoded = str(repo_dir).replace(os.sep, "-")
        return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"

    def _session_resumable(self, session_id: str, repo_dir: Path) -> bool:
        """True if the CLI's transcript file for this session still exists.

        Stored session ids can outlive their on-disk transcripts (manual /clear,
        pruning, deletion). Resuming a missing session causes the CLI to exit
        immediately with an `error_during_execution` result, which previously
        looped because the error result emits a fresh phantom session id.
        """
        return self._session_jsonl_path(session_id, repo_dir).is_file()

    def new_session(self, chat_id: int, repo: str) -> None:
        """Clear the session for this (chat, repo) pair so the next message starts fresh.

        Sessions for other repos in the same chat are preserved.
        """
        self._sessions.pop((chat_id, repo), None)
        # _last_models intentionally preserved: the alias hasn't changed, so the
        # CLI will resolve to the same id on the next turn. Cleared in
        # clear_last_model() when the user switches model via /model.

    async def abort(self, chat_id: int) -> bool:
        """Kill the running CLI subprocess for a chat. Returns True if a process was killed."""
        had_proc = chat_id in self._running_procs and self.has_running_proc(chat_id)
        # Cancel stream reader first so stdout EOF doesn't race with cleanup
        task = self._stream_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._kill_proc(chat_id)
        return had_proc

    def has_running_proc(self, chat_id: int) -> bool:
        """Check if a CLI subprocess is currently running for a chat."""
        proc = self._running_procs.get(chat_id)
        return proc is not None and proc.returncode is None

    def get_last_model(self, chat_id: int) -> str | None:
        """Return the resolved model id from the most recent CLI init event, if any."""
        return self._last_models.get(chat_id)

    def clear_last_model(self, chat_id: int) -> None:
        """Drop the cached resolved model — call this when the user switches alias."""
        self._last_models.pop(chat_id, None)

    async def probe_resolved_model(self, alias: str, timeout: float = 20.0) -> str | None:
        """Spawn a short-lived CLI process to resolve a model alias to its full id.

        The CLI only reports the resolved model in the system/init event that it
        emits after receiving a prompt on stdin. We send a trivial prompt, read
        until the init event, grab the model, and kill the process before it
        finishes the turn. Costs one tiny Claude request.

        Returns the resolved model id, or None if the probe failed.
        """
        if not self.available or not self.cli_path:
            return None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path,
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--model",
                alias,
                "--print",
                "--verbose",
                "--dangerously-skip-permissions",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdin is not None and proc.stdout is not None
            msg = json.dumps({"type": "user", "message": {"role": "user", "content": "ok"}})
            proc.stdin.write((msg + "\n").encode("utf-8"))
            await proc.stdin.drain()

            async def _read_until_init() -> str | None:
                assert proc is not None and proc.stdout is not None
                async for raw in proc.stdout:
                    try:
                        event = json.loads(raw.decode("utf-8", errors="replace").strip())
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        model_id = event.get("model")
                        if isinstance(model_id, str) and model_id:
                            return model_id
                return None

            return await asyncio.wait_for(_read_until_init(), timeout=timeout)
        except (TimeoutError, OSError) as e:
            logger.warning("Model probe for %s failed: %s", alias, e)
            return None
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except TimeoutError:
                    pass

    async def send_followup(self, chat_id: int, text: str) -> bool:
        """Send a follow-up message to a running CLI process via stdin.

        Returns True if the message was sent, False if no process is running.
        Uses a per-chat lock to prevent concurrent writes from interleaving.
        """
        stdin = self._proc_stdins.get(chat_id)
        if not stdin or stdin.is_closing():
            return False
        if not self.has_running_proc(chat_id):
            self._proc_stdins.pop(chat_id, None)
            return False

        lock = self._stdin_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            # Re-check after acquiring lock — process may have died while waiting
            if not self.has_running_proc(chat_id) or stdin.is_closing():
                self._proc_stdins.pop(chat_id, None)
                return False

            msg = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
            try:
                stdin.write((msg + "\n").encode("utf-8"))
                await asyncio.wait_for(stdin.drain(), timeout=5.0)
                logger.info("Sent follow-up to chat %d: %s", chat_id, text[:80])
                return True
            except TimeoutError:
                logger.warning("Stdin drain timed out for chat %d — process may be hung", chat_id)
                self._proc_stdins.pop(chat_id, None)
                return False
            except BrokenPipeError:
                logger.warning("Broken pipe sending follow-up to chat %d — process died", chat_id)
                self._proc_stdins.pop(chat_id, None)
                return False
            except Exception as e:
                logger.warning("Failed to send follow-up to chat %d: %s", chat_id, e)
                self._proc_stdins.pop(chat_id, None)
                return False

    async def interrupt(self, chat_id: int) -> bool:
        """Send a control_request interrupt to the running CLI process.

        Unlike abort(), this does NOT kill the process. It asks CC to stop
        the current turn while keeping the process and session alive. Uses
        the same protocol the Claude Agent SDK uses: a JSON control_request
        with subtype "interrupt" written to stdin.

        Returns True if the request was written, False if there's no live
        process or stdin was unavailable.
        """
        stdin = self._proc_stdins.get(chat_id)
        if not stdin or stdin.is_closing():
            return False
        if not self.has_running_proc(chat_id):
            self._proc_stdins.pop(chat_id, None)
            return False

        counter = self._control_request_counter.get(chat_id, 0) + 1
        self._control_request_counter[chat_id] = counter
        request_id = f"req_{counter}_{uuid.uuid4().hex[:8]}"
        payload = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "interrupt"},
        }

        lock = self._stdin_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            if not self.has_running_proc(chat_id) or stdin.is_closing():
                self._proc_stdins.pop(chat_id, None)
                return False
            try:
                stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                await asyncio.wait_for(stdin.drain(), timeout=5.0)
                logger.info("Sent interrupt control_request to chat %d (request_id=%s)", chat_id, request_id)
                return True
            except TimeoutError:
                logger.warning("Stdin drain timed out sending interrupt to chat %d", chat_id)
                return False
            except BrokenPipeError:
                logger.warning("Broken pipe sending interrupt to chat %d — process died", chat_id)
                self._proc_stdins.pop(chat_id, None)
                return False
            except Exception as e:
                logger.warning("Failed to send interrupt to chat %d: %s", chat_id, e)
                return False

    # ── CLI invocation ───────────────────────────────────────────────

    async def _kill_proc(self, chat_id: int) -> None:
        """Kill an existing CLI process for a chat and clean up."""
        self._proc_stdins.pop(chat_id, None)
        proc = self._running_procs.pop(chat_id, None)
        self._proc_repos.pop(chat_id, None)
        stderr_task = self._stderr_tasks.pop(chat_id, None)
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, chat_id: int) -> None:
        """Stream the CLI's stderr to the bot logger.

        Without a consumer, the stderr pipe can fill and block the CLI, and
        useful diagnostics ("No conversation found with session ID: …") get
        silently dropped — which is exactly how stale-resume bugs hid.
        """
        if proc.stderr is None:
            return
        try:
            async for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.warning("claude[%d] stderr: %s", chat_id, line)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Stderr reader for chat %d ended: %s", chat_id, e)

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

        merged_mcp = _build_mcp_config(repo_dir / ".mcp.json")
        if merged_mcp:
            cmd.extend(["--mcp-config", merged_mcp])

        session_key = (chat_id, repo)
        session_id = self._sessions.get(session_key)
        if session_id and not self._session_resumable(session_id, repo_dir):
            logger.info(
                "Stored session %s for chat %d on %s missing on disk; starting fresh",
                session_id,
                chat_id,
                repo,
            )
            self._sessions.pop(session_key, None)
            session_id = None
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            new_id = str(uuid.uuid4())
            cmd.extend(["--session-id", new_id])
            self._sessions[session_key] = new_id

        cmd.extend(
            [
                "--append-system-prompt",
                "You are responding to a user via Telegram chat, NOT a terminal. Important constraints:\n"
                "- The user can only see your final text responses — not tool calls, file diffs, or intermediate output.\n"
                "- Do NOT ask the user to run CLI commands or use terminal features.\n"
                "- When presenting plans, include the full plan text in your response so the user can read it.\n"
                "- Keep responses concise for mobile reading.\n"
                "- If you need user input, ask a clear question in your response text.\n"
                "\n"
                "Image and file delivery:\n"
                "- The Telegram bot detects `[SEND: path]` markers in your responses and delivers the file directly to the user's phone.\n"
                "- When an MCP tool returns an image or generates a file, save it to disk (e.g. /tmp/) and use [SEND: path] to deliver it — do NOT pass image data back through the API as vision content.\n"
                "- For render_view: always pass save_to='/tmp/screenshot.png' (or similar) so the image is written to disk. Never let render_view return raw image data into the conversation.\n"
                "- You can include multiple [SEND: path] markers for multiple files.\n"
                "\n"
                "Asking the user to choose:\n"
                "- Use `[ASK: question | option1 | option2 | ...]` to present the user with inline buttons and get their choice before continuing.\n"
                "- Format: the part before the first | is the question text; each subsequent | delimited part is one button option (2-5 options).\n"
                "- Example: [ASK: Which approach should I use? | Refactor in place | Create new module | Skip for now]\n"
                "- The user's selection will be sent back to you as the next message — wait for it before proceeding.\n"
                "- Do NOT use [ASK:] for yes/no confirmations that don't need user input; just proceed unless the decision is genuinely ambiguous.",
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
        assert proc.stdin is not None, "stdin pipe not available"
        self._proc_stdins[chat_id] = proc.stdin
        self._proc_repos[chat_id] = repo
        self._stderr_tasks[chat_id] = asyncio.create_task(self._drain_stderr(proc, chat_id))
        return proc

    # ── Stream mode (continuous) ─────────────────────────────────────

    def stream_mode_active(self, chat_id: int) -> bool:
        """Return True if a continuous stream reader is running for this chat."""
        task = self._stream_tasks.get(chat_id)
        return task is not None and not task.done()

    async def start_stream(
        self,
        chat_id: int,
        repo: str,
        on_event,
        branch: str | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
    ) -> None:
        """Launch CC and start a continuous reader that dispatches every stdout event.

        Unlike run(), this never stops on the result event — events emitted by
        scheduled wakeups, monitors, background agents, etc., flow to on_event
        as they arrive. Tear down with stop_stream().
        """
        # Tear down any prior stream for this chat
        await self.stop_stream(chat_id, kill_proc=True)

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
        task = asyncio.create_task(self._stream_forever(proc, chat_id, on_event))
        self._stream_tasks[chat_id] = task
        logger.info("Stream mode started for chat %d (repo=%s)", chat_id, repo)

    async def stop_stream(self, chat_id: int, kill_proc: bool = False) -> None:
        """Cancel the continuous reader. Optionally kill the CLI process too."""
        task = self._stream_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if kill_proc:
            await self._kill_proc(chat_id)

    async def feed(self, chat_id: int, text: str) -> bool:
        """Send a user message via stdin while in stream mode.

        Thin wrapper over send_followup — the stream reader handles the response.
        Returns True if the message was written, False if the process is gone.
        """
        return await self.send_followup(chat_id, text)

    async def _stream_forever(self, proc, chat_id: int, on_event) -> None:
        """Read stdout indefinitely, dispatching every event to on_event.

        Does not break on the result event — keeps reading so scheduled-wakeup
        events and other async output reach the chat. Exits on stdout EOF
        (process death) or cancellation.
        """
        assert proc.stdout is not None
        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")

                # Update manager-internal state from events
                if event_type == "result":
                    # Error results (e.g. resume of missing session) carry a
                    # fresh phantom session id we must NOT capture — doing so
                    # poisons the stored session and loops the failure.
                    sid = event.get("session_id")
                    if isinstance(sid, str) and sid and not event.get("is_error"):
                        repo = self._proc_repos.get(chat_id)
                        if repo:
                            self._sessions[(chat_id, repo)] = sid
                elif event_type == "system" and event.get("subtype") == "init":
                    model_id = event.get("model")
                    if isinstance(model_id, str) and model_id:
                        self._last_models[chat_id] = model_id

                try:
                    await on_event(event)
                except Exception as e:
                    logger.warning("Stream on_event handler raised for chat %d: %s", chat_id, e)

            # stdout EOF — process has exited
            logger.info("Stream reader: stdout EOF for chat %d", chat_id)
            self._proc_stdins.pop(chat_id, None)
            try:
                await on_event({"_type": "stream_end", "reason": "eof"})
            except Exception:
                pass
        except asyncio.CancelledError:
            logger.info("Stream reader cancelled for chat %d", chat_id)
            raise
        except Exception as e:
            logger.error("Stream reader error for chat %d: %s", chat_id, e, exc_info=True)
            try:
                await on_event({"_type": "stream_end", "reason": f"error: {e}"})
            except Exception:
                pass
