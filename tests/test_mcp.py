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


class TestTransportDispatch:
    """Tests for transport type detection and routing."""

    @pytest.mark.asyncio
    async def test_routes_to_stdio_with_command(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        config = {"command": "npx", "args": ["-y", "pkg"]}

        with patch.object(manager, "_connect_stdio_server", new_callable=AsyncMock) as mock_stdio:
            await manager._connect_server("test", config)
        mock_stdio.assert_called_once_with("test", config)

    @pytest.mark.asyncio
    async def test_routes_to_http_with_url(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        config = {"url": "https://mcp.example.com/mcp"}

        with patch.object(manager, "_connect_http_server", new_callable=AsyncMock) as mock_http:
            await manager._connect_server("test", config)
        mock_http.assert_called_once_with("test", config)

    @pytest.mark.asyncio
    async def test_routes_to_http_with_type_field(self):
        from mcp_tools import MCPManager

        manager = MCPManager()
        config = {"type": "http", "url": "https://mcp.example.com/mcp"}

        with patch.object(manager, "_connect_http_server", new_callable=AsyncMock) as mock_http:
            await manager._connect_server("test", config)
        mock_http.assert_called_once_with("test", config)

    @pytest.mark.asyncio
    async def test_defaults_to_stdio(self):
        """Config without url or type=http should default to stdio."""
        from mcp_tools import MCPManager

        manager = MCPManager()
        config = {"command": "python", "args": ["server.py"]}

        with patch.object(manager, "_connect_stdio_server", new_callable=AsyncMock) as mock_stdio:
            await manager._connect_server("test", config)
        mock_stdio.assert_called_once()


class TestConnectHttpServer:
    """Tests for HTTP transport connection."""

    @pytest.mark.asyncio
    async def test_connects_and_registers_tools(self):
        from mcp_tools import MCPManager

        manager = MCPManager()

        # Mock the streamable_http_client context manager
        mock_read = MagicMock()
        mock_write = MagicMock()
        mock_get_session_id = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=(mock_read, mock_write, mock_get_session_id))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        # Mock the session
        mock_session = AsyncMock()
        mock_tool = MagicMock()
        mock_tool.name = "search"
        mock_tool.description = "Search the web"
        mock_tool.inputSchema = {"type": "object", "properties": {"query": {"type": "string"}}}
        mock_list_result = MagicMock()
        mock_list_result.tools = [mock_tool]
        mock_session.list_tools.return_value = mock_list_result

        with (
            patch("mcp_tools.streamable_http_client", return_value=mock_ctx),
            patch("mcp_tools.ClientSession", return_value=mock_session),
        ):
            await manager._connect_http_server("remote", {"url": "https://mcp.example.com/mcp"})

        assert "remote" in manager._sessions
        assert len(manager._tools) == 1
        assert manager._tools[0]["name"] == "mcp_remote_search"
        assert manager._tool_to_server["mcp_remote_search"] == "remote"

    @pytest.mark.asyncio
    async def test_passes_headers_to_httpx(self):
        from mcp_tools import MCPManager

        manager = MCPManager()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_list_result = MagicMock()
        mock_list_result.tools = []
        mock_session.list_tools.return_value = mock_list_result

        headers = {"Authorization": "Bearer test-token", "X-Custom": "value"}

        with (
            patch("mcp_tools.streamable_http_client", return_value=mock_ctx) as mock_client_fn,
            patch("mcp_tools.ClientSession", return_value=mock_session),
            patch("mcp_tools.httpx.AsyncClient") as mock_httpx,
        ):
            await manager._connect_http_server("api", {"url": "https://api.example.com/mcp", "headers": headers})

        # Verify httpx.AsyncClient was created with headers
        mock_httpx.assert_called_once_with(headers=headers)
        # Verify streamable_http_client was called with the httpx client
        mock_client_fn.assert_called_once_with("https://api.example.com/mcp", http_client=mock_httpx.return_value)

    @pytest.mark.asyncio
    async def test_no_headers_no_httpx_client(self):
        from mcp_tools import MCPManager

        manager = MCPManager()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_list_result = MagicMock()
        mock_list_result.tools = []
        mock_session.list_tools.return_value = mock_list_result

        with (
            patch("mcp_tools.streamable_http_client", return_value=mock_ctx) as mock_client_fn,
            patch("mcp_tools.ClientSession", return_value=mock_session),
        ):
            await manager._connect_http_server("api", {"url": "https://api.example.com/mcp"})

        # No headers = no custom httpx client
        mock_client_fn.assert_called_once_with("https://api.example.com/mcp", http_client=None)

    @pytest.mark.asyncio
    async def test_connection_error_propagates(self):
        """HTTP connection errors should propagate to be caught by initialize()."""
        from mcp_tools import MCPManager

        manager = MCPManager()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))

        with (
            patch("mcp_tools.streamable_http_client", return_value=mock_ctx),
            pytest.raises(ConnectionError),
        ):
            await manager._connect_http_server("bad", {"url": "https://bad.example.com/mcp"})

    @pytest.mark.asyncio
    async def test_initialize_catches_http_error(self):
        """initialize() should catch and log HTTP connection failures."""
        from mcp_tools import MCPManager

        manager = MCPManager()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))

        config = {"remote": {"url": "https://bad.example.com/mcp"}}

        with patch("mcp_tools.streamable_http_client", return_value=mock_ctx):
            await manager.initialize(config)

        # Should not raise, just log warning
        assert len(manager._sessions) == 0
        assert len(manager._tools) == 0


class TestRegisterTools:
    """Tests for the shared tool registration helper."""

    @pytest.mark.asyncio
    async def test_registers_multiple_tools(self):
        from mcp_tools import MCPManager

        manager = MCPManager()

        mock_session = AsyncMock()
        tool1 = MagicMock(name="tool_a", description="Tool A", inputSchema={"type": "object", "properties": {}})
        tool1.name = "tool_a"  # MagicMock's name kwarg is special
        tool2 = MagicMock(name="tool_b", description="Tool B", inputSchema=None)
        tool2.name = "tool_b"
        mock_result = MagicMock()
        mock_result.tools = [tool1, tool2]
        mock_session.list_tools.return_value = mock_result

        await manager._register_tools("srv", mock_session)

        assert len(manager._tools) == 2
        assert manager._tools[0]["name"] == "mcp_srv_tool_a"
        assert manager._tools[1]["name"] == "mcp_srv_tool_b"
        # tool_b has no inputSchema, should get default
        assert manager._tools[1]["input_schema"] == {"type": "object", "properties": {}}

    @pytest.mark.asyncio
    async def test_registers_no_tools(self):
        from mcp_tools import MCPManager

        manager = MCPManager()

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.tools = []
        mock_session.list_tools.return_value = mock_result

        await manager._register_tools("empty", mock_session)

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

    def test_valid_http_config(self):
        from mcp_tools import load_mcp_config

        config = {"remote": {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer tok"}}}
        with patch("os.getenv", return_value=json.dumps(config)):
            result = load_mcp_config()
        assert result == config
        assert result["remote"]["url"] == "https://mcp.example.com/mcp"

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
