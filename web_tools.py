"""Web search tool that Claude can call via tool_use. Uses DuckDuckGo — no API key needed."""

import json
import logging

from ddgs import DDGS

logger = logging.getLogger(__name__)


class WebSearchClient:
    """DuckDuckGo search client. No API key, no limits."""

    def __init__(self):
        pass

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search the web and return results."""
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "content": r.get("body", ""),
            }
            for r in raw
        ]


WEB_TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web for current information. Use this for questions about recent events, documentation, error messages, or anything that benefits from up-to-date information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
]


def execute_tool(client: WebSearchClient, tool_name: str, tool_input: dict) -> str:
    """Execute a web search tool call and return the result as a string."""
    try:
        if tool_name == "web_search":
            max_results = min(tool_input.get("max_results", 5), 10)
            results = client.search(tool_input["query"], max_results)
            return json.dumps(results, indent=2)
        return f"Unknown tool: {tool_name}"
    except Exception as e:
        return f"Search error: {e}"
