"""Tests for web_tools.py."""

import json
from unittest.mock import MagicMock, patch


class TestWebSearchClient:
    def test_search_returns_results(self):
        from web_tools import WebSearchClient

        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = [
            {"title": "Result 1", "href": "https://example.com", "body": "Description 1"},
        ]
        with patch("web_tools.DDGS", return_value=mock_ddgs):
            client = WebSearchClient()
            results = client.search("test query", max_results=1)
        assert len(results) == 1
        assert results[0]["title"] == "Result 1"
        assert results[0]["url"] == "https://example.com"
        assert results[0]["content"] == "Description 1"

    def test_search_empty_results(self):
        from web_tools import WebSearchClient

        mock_ddgs = MagicMock()
        mock_ddgs.__enter__ = MagicMock(return_value=mock_ddgs)
        mock_ddgs.__exit__ = MagicMock(return_value=False)
        mock_ddgs.text.return_value = []
        with patch("web_tools.DDGS", return_value=mock_ddgs):
            client = WebSearchClient()
            results = client.search("obscure query")
        assert results == []


class TestExecuteTool:
    def test_web_search_tool(self):
        from web_tools import WebSearchClient, execute_tool

        client = MagicMock(spec=WebSearchClient)
        client.search.return_value = [{"title": "T", "url": "U", "content": "C"}]
        result = execute_tool(client, "web_search", {"query": "test"})
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["title"] == "T"

    def test_max_results_capped_at_10(self):
        from web_tools import WebSearchClient, execute_tool

        client = MagicMock(spec=WebSearchClient)
        client.search.return_value = []
        execute_tool(client, "web_search", {"query": "test", "max_results": 50})
        client.search.assert_called_once_with("test", 10)

    def test_unknown_tool(self):
        from web_tools import WebSearchClient, execute_tool

        client = MagicMock(spec=WebSearchClient)
        result = execute_tool(client, "nonexistent_tool", {})
        assert "Unknown tool" in result

    def test_error_handling(self):
        from web_tools import WebSearchClient, execute_tool

        client = MagicMock(spec=WebSearchClient)
        client.search.side_effect = RuntimeError("Network error")
        result = execute_tool(client, "web_search", {"query": "test"})
        assert "Search error" in result
