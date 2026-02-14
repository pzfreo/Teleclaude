"""Tests for github_tools.py — mocked HTTP."""

import base64
import json
from unittest.mock import MagicMock

import requests

from github_tools import GitHubClient


class TestTimeouts:
    """TODO #3: Verify all HTTP methods pass timeout."""

    def test_get_passes_timeout(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response({"default_branch": "main"})
        github_client.get_default_branch("owner/repo")
        _, kwargs = mock_github_session.get.call_args
        assert kwargs.get("timeout") == GitHubClient.DEFAULT_TIMEOUT

    def test_post_passes_timeout(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response({"object": {"sha": "abc"}})
        mock_github_session.post.return_value = mock_github_session._make_response({"ref": "refs/heads/new"})
        github_client.create_branch("owner/repo", "new", "main")
        _, kwargs = mock_github_session.post.call_args
        assert kwargs.get("timeout") == GitHubClient.DEFAULT_TIMEOUT

    def test_put_passes_timeout(self, github_client, mock_github_session):
        mock_github_session.put.return_value = mock_github_session._make_response({"content": {}})
        github_client.create_or_update_file("o/r", "f.py", "code", "msg", "main")
        _, kwargs = mock_github_session.put.call_args
        assert kwargs.get("timeout") == GitHubClient.DEFAULT_TIMEOUT

    def test_patch_passes_timeout(self, github_client, mock_github_session):
        mock_github_session.patch.return_value = mock_github_session._make_response({})
        github_client._patch("/test", json={})
        _, kwargs = mock_github_session.patch.call_args
        assert kwargs.get("timeout") == GitHubClient.DEFAULT_TIMEOUT

    def test_timeout_error_is_raised(self, github_client, mock_github_session):
        from github_tools import execute_tool

        mock_github_session.get.side_effect = requests.exceptions.Timeout("Connection timed out")
        result = execute_tool(github_client, "owner/repo", "get_file", {"path": "test.py"})
        assert "Error:" in result
        assert "timed out" in result.lower() or "Timeout" in result

    def test_delete_passes_timeout(self, github_client, mock_github_session):
        """delete_file uses session.delete directly — verify timeout."""
        mock_github_session.get.return_value = mock_github_session._make_response({"sha": "abc123"})
        mock_github_session.delete.return_value = mock_github_session._make_response({})
        github_client.delete_file("o/r", "f.py", "remove file", "main")
        _, kwargs = mock_github_session.delete.call_args
        assert kwargs.get("timeout") == GitHubClient.DEFAULT_TIMEOUT


class TestGitHubClient:
    def test_get_file(self, github_client, mock_github_session):
        content = base64.b64encode(b"print('hello')").decode()
        mock_github_session.get.return_value = mock_github_session._make_response({"content": content})
        result = github_client.get_file("owner/repo", "main.py")
        assert result == "print('hello')"

    def test_get_file_with_ref(self, github_client, mock_github_session):
        content = base64.b64encode(b"v2 code").decode()
        mock_github_session.get.return_value = mock_github_session._make_response({"content": content})
        result = github_client.get_file("owner/repo", "main.py", ref="feature-branch")
        assert result == "v2 code"
        # Verify ref was passed as param
        call_kwargs = mock_github_session.get.call_args
        assert call_kwargs[1]["params"]["ref"] == "feature-branch"

    def test_list_directory(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response(
            [
                {"name": "README.md", "type": "file", "path": "README.md"},
                {"name": "src", "type": "dir", "path": "src"},
            ]
        )
        result = github_client.list_directory("owner/repo")
        assert len(result) == 2
        assert result[0]["name"] == "README.md"
        assert result[1]["type"] == "dir"

    def test_list_user_repos(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response(
            [
                {"full_name": "owner/repo1", "description": "First", "pushed_at": "2026-01-01T00:00:00Z"},
                {"full_name": "owner/repo2", "description": None, "pushed_at": "2026-01-02T00:00:00Z"},
            ]
        )
        result = github_client.list_user_repos(limit=5)
        assert len(result) == 2
        assert result[0]["full_name"] == "owner/repo1"
        assert result[1]["description"] == ""  # None replaced with ""

    def test_get_default_branch(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response({"default_branch": "main"})
        assert github_client.get_default_branch("owner/repo") == "main"

    def test_list_branches(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response(
            [{"name": "main"}, {"name": "develop"}, {"name": "feature-x"}]
        )
        result = github_client.list_branches("owner/repo")
        assert result == ["main", "develop", "feature-x"]

    def test_get_file_sha(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response({"sha": "abc123"})
        assert github_client.get_file_sha("owner/repo", "file.py") == "abc123"

    def test_get_file_sha_not_found(self, github_client, mock_github_session):
        mock_github_session.get.return_value.raise_for_status.side_effect = requests.HTTPError()
        assert github_client.get_file_sha("owner/repo", "missing.py") is None

    def test_get_issue(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response(
            {
                "number": 42,
                "title": "Bug report",
                "body": "Something broke",
                "state": "open",
                "labels": [{"name": "bug"}, {"name": "priority"}],
                "html_url": "https://github.com/owner/repo/issues/42",
            }
        )
        result = github_client.get_issue("owner/repo", 42)
        assert result["number"] == 42
        assert result["labels"] == ["bug", "priority"]

    def test_search_code(self, github_client, mock_github_session):
        mock_github_session.get.return_value = mock_github_session._make_response(
            {
                "items": [
                    {"path": "src/main.py", "name": "main.py", "html_url": "https://github.com/..."},
                ]
            }
        )
        result = github_client.search_code("owner/repo", "def main")
        assert len(result) == 1
        assert result[0]["path"] == "src/main.py"


class TestExecuteTool:
    def test_get_file_dispatch(self, github_client, mock_github_session):
        from github_tools import execute_tool

        content = base64.b64encode(b"code here").decode()
        mock_github_session.get.return_value = mock_github_session._make_response({"content": content})
        result = execute_tool(github_client, "owner/repo", "get_file", {"path": "test.py"})
        assert "code here" in result

    def test_unknown_tool(self, github_client):
        from github_tools import execute_tool

        result = execute_tool(github_client, "owner/repo", "nonexistent", {})
        assert "Unknown tool" in result

    def test_list_directory_dispatch(self, github_client, mock_github_session):
        from github_tools import execute_tool

        mock_github_session.get.return_value = mock_github_session._make_response(
            [{"name": "file.py", "type": "file", "path": "file.py"}]
        )
        result = execute_tool(github_client, "owner/repo", "list_directory", {"path": ""})
        parsed = json.loads(result)
        assert len(parsed) == 1

    def test_http_error_handling(self, github_client, mock_github_session):
        from github_tools import execute_tool

        resp = MagicMock()
        resp.status_code = 404
        resp.text = "Not Found"
        mock_github_session.get.return_value.raise_for_status.side_effect = requests.HTTPError(response=resp)
        result = execute_tool(github_client, "owner/repo", "get_file", {"path": "missing.py"})
        assert "GitHub API error" in result

    def test_generic_error_handling(self, github_client, mock_github_session):
        from github_tools import execute_tool

        mock_github_session.get.side_effect = RuntimeError("connection lost")
        result = execute_tool(github_client, "owner/repo", "get_file", {"path": "test.py"})
        assert "Error:" in result
