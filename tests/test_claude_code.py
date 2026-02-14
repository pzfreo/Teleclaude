"""Tests for claude_code.py — mocked subprocess."""

from pathlib import Path

from claude_code import ClaudeCodeManager


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
