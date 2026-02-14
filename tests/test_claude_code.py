"""Tests for claude_code.py — mocked subprocess."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

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
