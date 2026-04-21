"""Tests for claude_code.py — mocked subprocess."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from claude_code import ClaudeCodeManager, _get_cli_timeout


class TestClaudeCodeManager:
    def test_workspace_path(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("owner/repo")
        assert path == tmp_path / "owner" / "repo"

    def test_workspace_path_nested(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("my-org/my-project")
        assert path == tmp_path / "my-org" / "my-project"

    def test_available_with_cli(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/bin/claude")
        assert mgr.available is True

    def test_not_available_without_cli(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path), cli_path=None)
        # cli_path falls back to shutil.which("claude") which may or may not exist
        # but if we explicitly set it to empty string via env, it would be falsy
        # Test the property logic
        mgr.cli_path = None
        assert mgr.available is False

    def test_session_management(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        assert mgr.get_session_id(1001) is None
        mgr._sessions[1001] = "session-abc"
        assert mgr.get_session_id(1001) == "session-abc"

    def test_new_session_clears(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._sessions[1001] = "session-abc"
        mgr.new_session(1001)
        assert mgr.get_session_id(1001) is None

    def test_new_session_noop_if_no_session(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr.new_session(9999)  # should not raise
        assert mgr.get_session_id(9999) is None

    def test_default_workspace_root(self):
        mgr = ClaudeCodeManager("fake-token")
        assert mgr.workspace_root == Path("workspaces")


class TestTokenNotInCloneUrl:
    """TODO #1: Verify GitHub token is never embedded in git clone URLs."""

    async def test_ensure_clone_url_does_not_contain_token(self, tmp_path):
        """Clone URL must use https://github.com/... without the token."""
        token = "ghp_SUPERSECRETTOKEN123"
        mgr = ClaudeCodeManager(token, workspace_root=str(tmp_path))

        # Patch _git so we can inspect the clone URL without running git
        with patch.object(mgr, "_git", new_callable=AsyncMock) as mock_git:
            await mgr.ensure_clone("owner/repo")

        # _git is called with (parent_dir, "clone", url, name)
        mock_git.assert_called_once()
        args = mock_git.call_args[0]
        assert args[1] == "clone"
        clone_url = args[2]
        assert token not in clone_url
        assert clone_url == "https://github.com/owner/repo.git"

    def test_git_env_uses_credential_helper(self, tmp_path):
        """_git_env() must pass token via GIT_CONFIG credential helper, not in URL."""
        token = "ghp_SECRET"
        mgr = ClaudeCodeManager(token, workspace_root=str(tmp_path))
        env = mgr._git_env()

        assert env.get("GIT_CONFIG_COUNT") == "1"
        assert env.get("GIT_CONFIG_KEY_0") == "credential.helper"
        # The credential helper value should contain the token
        assert token in env.get("GIT_CONFIG_VALUE_0", "")
        # But the token should NOT be in any URL-shaped value
        for key, val in env.items():
            if "github.com" in val:
                assert token not in val, f"Token found in URL-like env var {key}"

    def test_git_env_empty_token(self, tmp_path):
        """When no token is set, credential helper should not be configured."""
        mgr = ClaudeCodeManager("", workspace_root=str(tmp_path))
        env = mgr._git_env()
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env


class TestPathTraversal:
    """TODO #2: Directory sandboxing — workspace_path blocks traversal."""

    def test_dotdot_in_owner(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        with pytest.raises(ValueError, match="Path traversal blocked"):
            mgr.workspace_path("../../etc/passwd")

    def test_dotdot_in_repo_name(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        with pytest.raises(ValueError, match="Path traversal blocked"):
            mgr.workspace_path("owner/../../etc")

    def test_absolute_path_rejected(self, tmp_path):
        """Repo string that resolves outside workspace root should be blocked."""
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        # This should resolve outside workspace root
        with pytest.raises((ValueError, Exception)):
            mgr.workspace_path("../../../tmp/evil")

    def test_valid_repo_allowed(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("legit-org/legit-repo")
        assert str(path).startswith(str(tmp_path))

    def test_deeply_nested_valid_repo(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("org/repo-with-dashes")
        assert path == tmp_path / "org" / "repo-with-dashes"


class TestCliTimeout:
    """Test _get_cli_timeout() configuration."""

    def test_default_value(self):
        with patch.dict("os.environ", {}, clear=False):
            # Remove CLAUDE_CLI_TIMEOUT if present
            import os

            os.environ.pop("CLAUDE_CLI_TIMEOUT", None)
            assert _get_cli_timeout() == 600

    def test_custom_value(self):
        with patch.dict("os.environ", {"CLAUDE_CLI_TIMEOUT": "1200"}):
            assert _get_cli_timeout() == 1200

    def test_minimum_clamped(self):
        with patch.dict("os.environ", {"CLAUDE_CLI_TIMEOUT": "10"}):
            assert _get_cli_timeout() == 60  # clamped to minimum

    def test_invalid_string_falls_back(self):
        with patch.dict("os.environ", {"CLAUDE_CLI_TIMEOUT": "not-a-number"}):
            assert _get_cli_timeout() == 600


class _FakeStdout:
    """Async-iterable stdout replacement feeding pre-canned lines, with optional hang after drain."""

    def __init__(self, lines: list[bytes], hang: bool = False):
        self._lines = list(lines)
        self._hang = hang

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._hang:
            await asyncio.sleep(3600)  # simulate waiting for more CLI output
        raise StopAsyncIteration


class _FakeProc:
    """Minimal asyncio.subprocess.Process stand-in for stream tests."""

    def __init__(self, lines: list[bytes], hang_after: bool = False):
        self.stdout = _FakeStdout(lines, hang=hang_after)
        self.stdin = None
        self.returncode = None


class TestStreamMode:
    """Tests for continuous stream mode (start_stream / stop_stream / _stream_forever)."""

    def test_stream_mode_inactive_by_default(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        assert mgr.stream_mode_active(1001) is False

    async def test_stream_forever_dispatches_events_past_result(self, tmp_path):
        """Reader must NOT stop on the result event — later events (e.g. from
        scheduled wakeups) must still flow to on_event. This is the core reason
        stream mode exists."""
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "result", "session_id": "sess-42", "result": "done"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "post-result"}]}},
        ]
        lines = [(json.dumps(e) + "\n").encode() for e in events]
        proc = _FakeProc(lines)

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        await mgr._stream_forever(proc, 1001, on_event)

        types = [e.get("type") or e.get("_type") for e in received]
        # All three original events, plus the synthetic stream_end on EOF
        assert "assistant" in types
        assert "result" in types
        assert types.count("assistant") == 2  # one before result, one after
        assert "stream_end" in types
        # session id was captured from result event
        assert mgr.get_session_id(1001) == "sess-42"

    async def test_stream_forever_captures_model_from_init(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        events = [
            {"type": "system", "subtype": "init", "model": "claude-opus-4-7"},
        ]
        lines = [(json.dumps(e) + "\n").encode() for e in events]
        proc = _FakeProc(lines)

        async def on_event(event):
            pass

        await mgr._stream_forever(proc, 2002, on_event)
        assert mgr.get_last_model(2002) == "claude-opus-4-7"

    async def test_stream_forever_survives_handler_exception(self, tmp_path):
        """An exception in on_event must not crash the reader."""
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "two"}]}},
        ]
        lines = [(json.dumps(e) + "\n").encode() for e in events]
        proc = _FakeProc(lines)

        received: list[dict] = []
        calls = {"n": 0}

        async def on_event(event):
            calls["n"] += 1
            received.append(event)
            if calls["n"] == 1:
                raise RuntimeError("handler boom")

        await mgr._stream_forever(proc, 3003, on_event)
        # Second event still reached on_event despite first raising
        assert len(received) >= 2

    async def test_stream_forever_skips_invalid_json(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        lines = [
            b"not valid json\n",
            (json.dumps({"type": "assistant", "message": {"content": []}}) + "\n").encode(),
        ]
        proc = _FakeProc(lines)

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        await mgr._stream_forever(proc, 4004, on_event)
        # Garbage line skipped; valid event dispatched; stream_end appended on EOF
        types = [e.get("type") or e.get("_type") for e in received]
        assert types.count("assistant") == 1
        assert "stream_end" in types

    async def test_stream_forever_cancellation(self, tmp_path):
        """Reader must exit cleanly on task cancellation (no stream_end emitted)."""
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        # hang_after=True: stdout yields nothing, then sleeps forever — simulates live CLI
        proc = _FakeProc([], hang_after=True)

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        task = asyncio.create_task(mgr._stream_forever(proc, 5005, on_event))
        await asyncio.sleep(0.05)  # let the reader enter the loop
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # No stream_end — cancellation path must not dispatch synthetic event
        assert not any(e.get("_type") == "stream_end" for e in received)

    async def test_start_stream_activates_and_stop_deactivates(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        # Bypass workspace prep and proc launch — we're testing the bookkeeping
        proc = _FakeProc([], hang_after=True)
        mgr._running_procs[7007] = proc  # type: ignore[assignment]

        with (
            patch.object(mgr, "ensure_clone", new=AsyncMock(return_value=Path("/tmp"))),
            patch.object(mgr, "pull_latest", new=AsyncMock()),
            patch.object(mgr, "_ensure_proc", new=AsyncMock(return_value=proc)),
            patch.object(mgr, "_kill_proc", new=AsyncMock()),
        ):
            await mgr.start_stream(7007, "owner/repo", on_event=AsyncMock())
            assert mgr.stream_mode_active(7007) is True
            await mgr.stop_stream(7007, kill_proc=True)
            assert mgr.stream_mode_active(7007) is False

    async def test_abort_cancels_stream_reader(self, tmp_path):
        """/stop path — abort() must tear down the reader task as well as the proc."""
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        proc = _FakeProc([], hang_after=True)
        mgr._running_procs[8008] = proc  # type: ignore[assignment]

        async def on_event(event):
            pass

        task = asyncio.create_task(mgr._stream_forever(proc, 8008, on_event))
        mgr._stream_tasks[8008] = task
        await asyncio.sleep(0.02)

        with patch.object(mgr, "_kill_proc", new=AsyncMock()) as mock_kill:
            await mgr.abort(8008)

        assert task.cancelled() or task.done()
        assert 8008 not in mgr._stream_tasks
        mock_kill.assert_awaited_once_with(8008)

    async def test_feed_delegates_to_send_followup(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        with patch.object(mgr, "send_followup", new=AsyncMock(return_value=True)) as mock_followup:
            result = await mgr.feed(9009, "hello")
        assert result is True
        mock_followup.assert_awaited_once_with(9009, "hello")


class _FakeStdin:
    """Minimal StreamWriter stand-in that captures written bytes."""

    def __init__(self):
        self.written = b""
        self._closing = False

    def is_closing(self) -> bool:
        return self._closing

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closing = True


class TestInterrupt:
    """Tests for the interrupt() method — soft cancel without killing the proc."""

    async def test_no_proc_returns_false(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        result = await mgr.interrupt(1111)
        assert result is False

    async def test_stdin_closed_returns_false(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        stdin = _FakeStdin()
        stdin.close()
        mgr._proc_stdins[1111] = stdin  # type: ignore[assignment]
        result = await mgr.interrupt(1111)
        assert result is False

    async def test_writes_control_request_interrupt(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        stdin = _FakeStdin()
        proc = _FakeProc([])
        mgr._proc_stdins[2222] = stdin  # type: ignore[assignment]
        mgr._running_procs[2222] = proc  # type: ignore[assignment]

        result = await mgr.interrupt(2222)
        assert result is True
        # Validate wire format — must be a control_request with subtype interrupt
        line = stdin.written.decode().strip()
        payload = json.loads(line)
        assert payload["type"] == "control_request"
        assert payload["request"]["subtype"] == "interrupt"
        assert "request_id" in payload and payload["request_id"].startswith("req_")

    async def test_does_not_kill_proc(self, tmp_path):
        """interrupt() must NOT kill the process — that's abort()'s job."""
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        stdin = _FakeStdin()
        proc = _FakeProc([])
        mgr._proc_stdins[3333] = stdin  # type: ignore[assignment]
        mgr._running_procs[3333] = proc  # type: ignore[assignment]

        await mgr.interrupt(3333)
        # Process stays in _running_procs — session preserved
        assert 3333 in mgr._running_procs
        assert 3333 in mgr._proc_stdins

    async def test_broken_pipe_returns_false_and_clears_stdin(self, tmp_path):
        mgr = ClaudeCodeManager("fake-token", workspace_root=str(tmp_path))
        proc = _FakeProc([])

        class _BrokenStdin(_FakeStdin):
            def write(self, data):
                raise BrokenPipeError("dead")

        stdin = _BrokenStdin()
        mgr._proc_stdins[4444] = stdin  # type: ignore[assignment]
        mgr._running_procs[4444] = proc  # type: ignore[assignment]

        result = await mgr.interrupt(4444)
        assert result is False
        assert 4444 not in mgr._proc_stdins
