"""Tests for MCP server support (mcp_tools.py)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMCPManager:
    """Tests for MCPManager class."""

    def test_empty_tools_before_init(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        assert manager.tools == []

    @pytest.mark.asyncio
    async def test_call_tool_unknown(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        result = await manager.call_tool("mcp_fake_tool", {})
        assert "Unknown MCP tool" in result

    @pytest.mark.asyncio
    async def test_call_tool_routes_correctly(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        # Manually set up a mock session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_content = MagicMock()
        mock_content.text = "Tool output"
        mock_result.content = [mock_content]
        mock_session.call_tool.return_value = mock_result

        manager._sessions["myserver"] = mock_session
        manager._tool_to_server["mcp_myserver_mytool"] = "myserver"

        result = await manager.call_tool("mcp_myserver_mytool", {"arg": "value"})
        assert result == "Tool output"
        mock_session.call_tool.assert_called_once_with("mytool", {"arg": "value"})

    @pytest.mark.asyncio
    async def test_call_tool_strips_prefix(self):
        """Verify the prefix is correctly stripped when routing to the server."""
        from mcp_tools import MCPManager

        manager = MCPManager()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool.return_value = mock_result

        manager._sessions["srv"] = mock_session
        manager._tool_to_server["mcp_srv_do_thing"] = "srv"

        await manager.call_tool("mcp_srv_do_thing", {})
        # Should strip "mcp_srv_" and call "do_thing"
        mock_session.call_tool.assert_called_once_with("do_thing", {})

    @pytest.mark.asyncio
    async def test_call_tool_error_handling(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = RuntimeError("Connection lost")

        manager._sessions["srv"] = mock_session
        manager._tool_to_server["mcp_srv_broken"] = "srv"

        result = await manager.call_tool("mcp_srv_broken", {})
        assert "MCP tool error" in result

    @pytest.mark.asyncio
    async def test_shutdown(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        ctx1 = AsyncMock()
        ctx2 = AsyncMock()
        manager._contexts = [ctx1, ctx2]
        manager._sessions["a"] = AsyncMock()

        await manager.shutdown()
        assert len(manager._contexts) == 0
        assert len(manager._sessions) == 0
        assert len(manager._tools) == 0


class TestLoadMCPConfig:
    """Tests for load_mcp_config()."""

    def test_no_env_var(self):
        from mcp_tools import load_mcp_config

        with patch.dict("os.environ", {}, clear=True), patch("os.getenv", return_value=""):
            result = load_mcp_config()
        assert result is None

    def test_valid_json(self):
        from mcp_tools import load_mcp_config

        config = {"myserver": {"command": "npx", "args": ["-y", "pkg"]}}
        with patch("os.getenv", return_value=json.dumps(config)):
            result = load_mcp_config()
        assert result == config

    def test_invalid_json(self):
        from mcp_tools import load_mcp_config

        with patch("os.getenv", return_value="not json"):
            result = load_mcp_config()
        assert result is None

    def test_non_dict_json(self):
        from mcp_tools import load_mcp_config

        with patch("os.getenv", return_value="[1, 2, 3]"):
            result = load_mcp_config()
        assert result is None
