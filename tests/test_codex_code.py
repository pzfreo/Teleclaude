"""Tests for codex_code.py — mocked subprocess."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from codex_code import CodexCodeManager, format_item_progress, looks_like_auth_error


class TestCodexCodeManager:
    def test_workspace_path(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("owner/repo")
        assert path == tmp_path / "owner" / "repo"

    def test_workspace_path_nested(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("my-org/my-project")
        assert path == tmp_path / "my-org" / "my-project"

    def test_available_with_cli(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        assert mgr.available is True

    def test_not_available_without_cli(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path=None)
        mgr.cli_path = None
        assert mgr.available is False

    def test_session_management(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        assert mgr.get_session_id(1001, "owner/repo") is None
        mgr._sessions[(1001, "owner/repo")] = "thread-abc"
        assert mgr.get_session_id(1001, "owner/repo") == "thread-abc"

    def test_new_session_clears(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._sessions[(1001, "owner/repo")] = "thread-abc"
        mgr.new_session(1001, "owner/repo")
        assert mgr.get_session_id(1001, "owner/repo") is None

    def test_new_session_noop_if_no_session(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr.new_session(9999, "owner/repo")  # should not raise
        assert mgr.get_session_id(9999, "owner/repo") is None

    def test_sessions_isolated_per_repo(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._sessions[(1001, "owner/repo-a")] = "thread-a"
        mgr._sessions[(1001, "owner/repo-b")] = "thread-b"
        mgr.new_session(1001, "owner/repo-a")
        assert mgr.get_session_id(1001, "owner/repo-a") is None
        assert mgr.get_session_id(1001, "owner/repo-b") == "thread-b"

    def test_default_workspace_root(self):
        mgr = CodexCodeManager("fake-token")
        assert mgr.workspace_root == Path("workspaces-codex")

    def test_has_running_proc_false_by_default(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        assert mgr.has_running_proc(1001) is False

    async def test_abort_no_proc_returns_false(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        assert await mgr.abort(1001) is False


class TestTokenNotInCloneUrl:
    async def test_ensure_clone_url_does_not_contain_token(self, tmp_path):
        token = "ghp_SUPERSECRETTOKEN123"
        mgr = CodexCodeManager(token, workspace_root=str(tmp_path))

        with patch.object(mgr, "_git", new_callable=AsyncMock) as mock_git:
            await mgr.ensure_clone("owner/repo")

        mock_git.assert_called_once()
        args = mock_git.call_args[0]
        assert args[1] == "clone"
        clone_url = args[2]
        assert token not in clone_url
        assert clone_url == "https://github.com/owner/repo.git"

    def test_git_env_uses_credential_helper(self, tmp_path):
        token = "ghp_SECRET"
        mgr = CodexCodeManager(token, workspace_root=str(tmp_path))
        env = mgr._git_env()

        assert env.get("GIT_CONFIG_COUNT") == "1"
        assert env.get("GIT_CONFIG_KEY_0") == "credential.helper"
        assert token in env.get("GIT_CONFIG_VALUE_0", "")

    def test_git_env_empty_token(self, tmp_path):
        mgr = CodexCodeManager("", workspace_root=str(tmp_path))
        env = mgr._git_env()
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env

    async def test_sanitize_remote_rewrites_tainted_url(self, tmp_path):
        token = "ghp_LEAKEDTOKEN"
        mgr = CodexCodeManager(token, workspace_root=str(tmp_path))
        path = tmp_path / "owner" / "repo"
        (path / ".git").mkdir(parents=True)

        calls: list[tuple] = []

        async def fake_git(cwd, *args):
            calls.append((cwd, args))
            if args[:3] == ("remote", "get-url", "origin"):
                return f"https://x-access-token:{token}@github.com/owner/repo.git"
            return ""

        with patch.object(mgr, "_git", side_effect=fake_git):
            await mgr.ensure_clone("owner/repo")

        set_url_calls = [c for c in calls if c[1][:3] == ("remote", "set-url", "origin")]
        assert len(set_url_calls) == 1
        assert set_url_calls[0][1][3] == "https://github.com/owner/repo.git"


class TestPathTraversal:
    def test_dotdot_in_owner(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        with pytest.raises(ValueError, match="Path traversal blocked"):
            mgr.workspace_path("../../etc/passwd")

    def test_valid_repo_allowed(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("legit-org/legit-repo")
        assert str(path).startswith(str(tmp_path))


class TestFormatItemProgress:
    def test_command_execution_in_progress(self):
        line = format_item_progress({"type": "command_execution", "command": "echo hi", "status": "in_progress"})
        assert line == "$ echo hi"

    def test_command_execution_completed_success_is_silent(self):
        line = format_item_progress(
            {"type": "command_execution", "command": "echo hi", "status": "completed", "exit_code": 0}
        )
        assert line is None

    def test_command_execution_completed_failure_reported(self):
        line = format_item_progress(
            {"type": "command_execution", "command": "false", "status": "completed", "exit_code": 1}
        )
        assert line == "$ command exited 1"

    def test_agent_message_is_not_a_progress_line(self):
        assert format_item_progress({"type": "agent_message", "text": "hello"}) is None

    def test_error_item(self):
        line = format_item_progress({"type": "error", "message": "boom"})
        assert line == "⚠️ boom"

    def test_unknown_item_type(self):
        assert format_item_progress({"type": "reasoning", "text": "thinking..."}) is None


class TestLooksLikeAuthError:
    def test_none_is_false(self):
        assert looks_like_auth_error(None) is False

    def test_specific_phrase_matches(self):
        assert looks_like_auth_error("Please run `codex login` to continue") is True

    def test_bare_401_without_context_is_false(self):
        assert looks_like_auth_error("the meeting room is 401") is False

    def test_401_with_api_context_is_true(self):
        assert looks_like_auth_error('{"type":"error","status":401,"error":{"type":"invalid_request_error"}}') is True


class TestRunTurn:
    """run_turn spawns one subprocess per call — mocked at asyncio.create_subprocess_exec."""

    @staticmethod
    def _fake_proc(stdout_lines: list[bytes], returncode: int = 0):
        class _FakeStream:
            def __init__(self, lines):
                self._lines = list(lines)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._lines:
                    return self._lines.pop(0)
                raise StopAsyncIteration

        class _FakeProc:
            def __init__(self):
                self.stdout = _FakeStream(stdout_lines)
                self.stderr = _FakeStream([])
                self.returncode = returncode

            async def wait(self):
                return self.returncode

        return _FakeProc()

    async def test_captures_thread_id_and_builds_fresh_command(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        events = [
            {"type": "thread.started", "thread_id": "thread-123"},
            {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "hi"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10}},
        ]
        lines = [(json.dumps(e) + "\n").encode() for e in events]
        proc = self._fake_proc(lines)

        captured: dict[str, list] = {}

        async def fake_create(*args, **_kwargs):
            captured["cmd"] = list(args)
            return proc

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/local/bin/codex"
        assert "exec" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "resume" not in cmd
        assert cmd[-1] == "hello"
        assert mgr.get_session_id(1001, "owner/repo") == "thread-123"
        assert len(received) == 3

    async def test_resume_uses_stored_session_id(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)
        mgr._sessions[(1001, "owner/repo")] = "thread-existing"

        proc = self._fake_proc([])
        captured: dict[str, list] = {}

        async def fake_create(*args, **_kwargs):
            captured["cmd"] = list(args)
            return proc

        async def on_event(event):
            pass

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "follow up", on_event)

        cmd = captured["cmd"]
        idx = cmd.index("resume")
        assert cmd[idx + 1] == "thread-existing"
        assert cmd[idx + 2] == "follow up"

    async def test_nonzero_exit_emits_process_error_event(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        proc = self._fake_proc([], returncode=1)

        async def fake_create(*args, **_kwargs):
            return proc

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert any(e.get("type") == "_process_error" for e in received)

    async def test_running_proc_cleared_after_turn(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)
        proc = self._fake_proc([])

        async def fake_create(*args, **_kwargs):
            return proc

        async def on_event(event):
            pass

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert mgr.has_running_proc(1001) is False
