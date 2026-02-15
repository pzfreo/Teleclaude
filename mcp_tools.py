"""MCP (Model Context Protocol) server support for Teleclaude.

Connects to MCP servers via stdio transport, discovers their tools,
and converts them to Anthropic tool-use format for the API bot.
"""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# MCP SDK imports — gracefully handled by the loading block in bot.py
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPManager:
    """Manages connections to MCP servers and routes tool calls."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[dict[str, Any]] = []
        self._tool_to_server: dict[str, str] = {}
        self._contexts: list[Any] = []  # Keep references to prevent GC

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Return Anthropic-format tool definitions for all connected servers."""
        return self._tools

    async def initialize(self, config: dict[str, Any]) -> None:
        """Connect to all configured MCP servers and discover tools.

        Config format (Claude Desktop style):
        {
            "server_name": {
                "command": "npx",
                "args": ["-y", "package"],
                "env": {}
            }
        }
        """
        for server_name, server_config in config.items():
            try:
                await self._connect_server(server_name, server_config)
            except Exception as e:
                logger.warning("MCP server '%s' failed to connect: %s", server_name, e)

        logger.info(
            "MCP initialized: %d server(s), %d tool(s)",
            len(self._sessions),
            len(self._tools),
        )

    async def _connect_server(self, name: str, config: dict[str, Any]) -> None:
        """Connect to a single MCP server and register its tools."""
        command = config.get("command", "")
        args = config.get("args", [])
        env = config.get("env")

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        # Create the stdio client context
        ctx = stdio_client(server_params)
        streams = await ctx.__aenter__()
        self._contexts.append(ctx)

        # Create and initialize the session
        session = ClientSession(*streams)
        await session.__aenter__()
        self._contexts.append(session)
        await session.initialize()

        self._sessions[name] = session

        # Discover tools and convert to Anthropic format
        result = await session.list_tools()
        for tool in result.tools:
            prefixed_name = f"mcp_{name}_{tool.name}"
            anthropic_tool: dict[str, Any] = {
                "name": prefixed_name,
                "description": tool.description or f"MCP tool: {tool.name} from {name}",
                "input_schema": tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}},
            }
            self._tools.append(anthropic_tool)
            self._tool_to_server[prefixed_name] = name
            logger.info("MCP tool registered: %s (from %s)", prefixed_name, name)

    async def call_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Route a tool call to the correct MCP server."""
        server_name = self._tool_to_server.get(tool_name)
        if not server_name:
            return f"Unknown MCP tool: {tool_name}"

        session = self._sessions.get(server_name)
        if not session:
            return f"MCP server '{server_name}' is not connected."

        # Strip the mcp_{server}_ prefix to get the original tool name
        prefix = f"mcp_{server_name}_"
        original_name = tool_name[len(prefix) :]

        try:
            result = await session.call_tool(original_name, tool_input)
            # Concatenate text content from the result
            texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    texts.append(content.text)
                else:
                    texts.append(str(content))
            return "\n".join(texts) if texts else "(empty result)"
        except Exception as e:
            logger.error("MCP tool '%s' failed: %s", tool_name, e)
            return f"MCP tool error: {e}"

    async def shutdown(self) -> None:
        """Close all MCP server connections."""
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("Error closing MCP context: %s", e)
        self._contexts.clear()
        self._sessions.clear()
        self._tools.clear()
        self._tool_to_server.clear()


def load_mcp_config() -> dict[str, Any] | None:
    """Load MCP server configuration from MCP_SERVERS env var."""
    raw = os.getenv("MCP_SERVERS", "")
    if not raw:
        return None
    try:
        config = json.loads(raw)
        if not isinstance(config, dict):
            logger.warning("MCP_SERVERS must be a JSON object, got %s", type(config).__name__)
            return None
        return config
    except json.JSONDecodeError as e:
        logger.warning("MCP_SERVERS is not valid JSON: %s", e)
        return None
