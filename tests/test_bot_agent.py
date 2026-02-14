"""Tests for bot_agent.py — command handlers and message handling."""

from unittest.mock import AsyncMock, MagicMock, patch


class TestFormatToolProgress:
    """Test _format_tool_progress() formatting."""

    def test_read_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Read", "input": {"file_path": "/app/src/main.py"}})
        assert result == "Reading app/src/main.py"

    def test_write_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Write", "input": {"file_path": "/app/src/main.py"}})
        assert result == "Writing app/src/main.py"

    def test_edit_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Edit", "input": {"file_path": "/a/b/c.py"}})
        assert result == "Editing a/b/c.py"

    def test_bash_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Bash", "input": {"command": "npm test"}})
        assert result == "$ npm test"

    def test_bash_multiline_truncated(self):
        from bot_agent import _format_tool_progress

        long_cmd = "a" * 100 + "\nsecond line"
        result = _format_tool_progress({"name": "Bash", "input": {"command": long_cmd}})
        assert result.startswith("$ ")
        assert len(result) <= 82  # "$ " + 80 chars

    def test_glob_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Glob", "input": {"pattern": "**/*.py"}})
        assert result == "Finding **/*.py"

    def test_grep_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Grep", "input": {"pattern": "TODO"}})
        assert result == "Searching: TODO"

    def test_task_tool(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Task", "input": {"description": "explore code"}})
        assert result == "Subagent: explore code"

    def test_unknown_tool_fallback(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "web_fetch", "input": {}})
        assert result == "Web Fetch"

    def test_empty_name(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "", "input": {}})
        assert result is None

    def test_empty_input(self):
        from bot_agent import _format_tool_progress

        result = _format_tool_progress({"name": "Read", "input": {}})
        assert result is None


class TestShortPath:
    def test_long_path(self):
        from bot_agent import _short_path

        # _short_path keeps last 3 components for paths with >3 parts
        assert _short_path("/home/user/projects/src/main.py") == "projects/src/main.py"

    def test_short_path(self):
        from bot_agent import _short_path

        assert _short_path("src/main.py") == "src/main.py"

    def test_single_component(self):
        from bot_agent import _short_path

        assert _short_path("file.py") == "file.py"


class TestSaveAttachment:
    def test_sanitizes_path_traversal_in_label(self, tmp_path):
        with (
            patch("bot_agent.claude_code_mgr") as mock_mgr,
        ):
            mock_mgr.workspace_root = tmp_path
            from bot_agent import _save_attachment

            path = _save_attachment(123, b"hello", "text/plain", "../../etc/passwd")
            # The path traversal characters should be sanitized (replaced with _)
            assert ".." not in path
            # File should be inside the shared dir, not outside it
            shared_dir = str((tmp_path / ".shared" / "123").resolve())
            assert path.startswith(shared_dir)

    def test_saves_file(self, tmp_path):
        with patch("bot_agent.claude_code_mgr") as mock_mgr:
            mock_mgr.workspace_root = tmp_path
            from bot_agent import _save_attachment

            path = _save_attachment(42, b"test data", "image/jpeg", "photo")
            import os

            assert os.path.exists(path)
            with open(path, "rb") as f:
                assert f.read() == b"test data"
