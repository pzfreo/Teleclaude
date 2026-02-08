"""Web search tool that Claude can call via tool_use."""

import json
import logging

import requests

logger = logging.getLogger(__name__)


class WebSearchClient:
    """Tavily web search client."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base = "https://api.tavily.com"

    def search(self, query: str, max_results: int = 5) -> list[dict]:
        """Search the web and return results."""
        resp = requests.post(
            f"{self.base}/search",
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        results = []

        # Include Tavily's AI-generated answer if available
        if data.get("answer"):
            results.append({"type": "answer", "content": data["answer"]})

        for item in data.get("results", []):
            results.append(
                {
                    "type": "result",
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", ""),
                }
            )

        return results


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
    except requests.HTTPError as e:
        return f"Search API error ({e.response.status_code}): {e.response.text[:500]}"
    except Exception as e:
        return f"Error: {e}"
