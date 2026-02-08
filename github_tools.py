"""GitHub API tools that Claude can call via tool_use."""

import base64
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class GitHubClient:
    """Thin wrapper around GitHub's REST API."""

    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            }
        )
        self.base = "https://api.github.com"

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        resp = self.session.get(f"{self.base}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: dict) -> dict:
        resp = self.session.post(f"{self.base}{path}", json=json)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json: dict) -> dict:
        resp = self.session.put(f"{self.base}{path}", json=json)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, json: dict) -> dict:
        resp = self.session.patch(f"{self.base}{path}", json=json)
        resp.raise_for_status()
        return resp.json()

    # ── Tools ────────────────────────────────────────────────────────

    def get_file(self, repo: str, path: str, ref: str | None = None) -> str:
        """Read a file from a repo. Returns its text content."""
        params = {}
        if ref:
            params["ref"] = ref
        data = self._get(f"/repos/{repo}/contents/{path}", params)
        content = base64.b64decode(data["content"]).decode()
        return content

    def list_directory(self, repo: str, path: str = "", ref: str | None = None) -> list[dict]:
        """List files/dirs at a path. Returns name, type, path for each entry."""
        params = {}
        if ref:
            params["ref"] = ref
        items = self._get(f"/repos/{repo}/contents/{path}", params)
        return [{"name": i["name"], "type": i["type"], "path": i["path"]} for i in items]

    def create_or_update_file(
        self, repo: str, path: str, content: str, message: str, branch: str, sha: str | None = None
    ) -> dict:
        """Create or update a file. Provide sha to update an existing file."""
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        return self._put(f"/repos/{repo}/contents/{path}", json=payload)

    def get_file_sha(self, repo: str, path: str, ref: str | None = None) -> str | None:
        """Get the SHA of a file (needed for updates). Returns None if not found."""
        try:
            params = {}
            if ref:
                params["ref"] = ref
            data = self._get(f"/repos/{repo}/contents/{path}", params)
            return data.get("sha")
        except requests.HTTPError:
            return None

    def create_branch(self, repo: str, branch_name: str, from_branch: str = "main") -> dict:
        """Create a new branch from an existing one."""
        ref_data = self._get(f"/repos/{repo}/git/ref/heads/{from_branch}")
        sha = ref_data["object"]["sha"]
        return self._post(f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch_name}", "sha": sha})

    def create_pull_request(
        self, repo: str, title: str, body: str, head: str, base: str = "main"
    ) -> dict:
        """Create a pull request."""
        data = self._post(
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        return {"number": data["number"], "url": data["html_url"], "title": data["title"]}

    def list_issues(self, repo: str, state: str = "open", limit: int = 10) -> list[dict]:
        """List issues on a repo."""
        items = self._get(f"/repos/{repo}/issues", params={"state": state, "per_page": limit})
        return [
            {"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]}
            for i in items
            if "pull_request" not in i
        ]

    def get_issue(self, repo: str, number: int) -> dict:
        """Get details of a specific issue."""
        data = self._get(f"/repos/{repo}/issues/{number}")
        return {
            "number": data["number"],
            "title": data["title"],
            "body": data.get("body", ""),
            "state": data["state"],
            "labels": [l["name"] for l in data.get("labels", [])],
            "url": data["html_url"],
        }

    def list_pull_requests(self, repo: str, state: str = "open", limit: int = 10) -> list[dict]:
        """List pull requests."""
        items = self._get(f"/repos/{repo}/pulls", params={"state": state, "per_page": limit})
        return [
            {"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]}
            for i in items
        ]

    def search_code(self, repo: str, query: str) -> list[dict]:
        """Search for code in a repo."""
        data = self._get("/search/code", params={"q": f"{query} repo:{repo}"})
        return [
            {"path": i["path"], "name": i["name"], "url": i["html_url"]}
            for i in data.get("items", [])[:10]
        ]

    def get_default_branch(self, repo: str) -> str:
        """Get the default branch name for a repo."""
        data = self._get(f"/repos/{repo}")
        return data["default_branch"]

    def list_branches(self, repo: str) -> list[str]:
        """List branches on a repo."""
        items = self._get(f"/repos/{repo}/branches", params={"per_page": 30})
        return [i["name"] for i in items]

    def delete_file(self, repo: str, path: str, message: str, branch: str) -> str:
        """Delete a file from the repo."""
        sha = self.get_file_sha(repo, path, ref=branch)
        if not sha:
            return f"File not found: {path}"
        resp = self.session.delete(
            f"{self.base}/repos/{repo}/contents/{path}",
            json={"message": message, "sha": sha, "branch": branch},
        )
        resp.raise_for_status()
        return f"Deleted {path} on {branch}"

    def get_tree(self, repo: str, ref: str = "HEAD", recursive: bool = True) -> list[dict]:
        """Get the full file tree of a repo (fast way to see all files)."""
        data = self._get(
            f"/repos/{repo}/git/trees/{ref}",
            params={"recursive": "1"} if recursive else None,
        )
        return [
            {"path": i["path"], "type": i["type"], "size": i.get("size", 0)}
            for i in data.get("tree", [])
            if i["type"] == "blob"
        ]

    def list_workflow_runs(self, repo: str, branch: str | None = None, limit: int = 5) -> list[dict]:
        """List recent GitHub Actions workflow runs."""
        params: dict = {"per_page": limit}
        if branch:
            params["branch"] = branch
        data = self._get(f"/repos/{repo}/actions/runs", params=params)
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "conclusion": r["conclusion"],
                "branch": r["head_branch"],
                "url": r["html_url"],
                "created_at": r["created_at"],
            }
            for r in data.get("workflow_runs", [])[:limit]
        ]

    def get_workflow_run(self, repo: str, run_id: int) -> dict:
        """Get details of a specific workflow run."""
        data = self._get(f"/repos/{repo}/actions/runs/{run_id}")
        return {
            "id": data["id"],
            "name": data["name"],
            "status": data["status"],
            "conclusion": data["conclusion"],
            "branch": data["head_branch"],
            "url": data["html_url"],
            "created_at": data["created_at"],
        }

    def get_workflow_run_logs(self, repo: str, run_id: int) -> str:
        """Get the log output of a failed workflow run's jobs."""
        jobs_data = self._get(f"/repos/{repo}/actions/runs/{run_id}/jobs")
        output_lines = []
        for job in jobs_data.get("jobs", []):
            output_lines.append(f"Job: {job['name']} — {job['conclusion']}")
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    output_lines.append(f"  FAILED step: {step['name']}")
        return "\n".join(output_lines) if output_lines else "No job details available."

    def trigger_workflow(self, repo: str, workflow_id: str, ref: str = "main", inputs: dict | None = None) -> str:
        """Trigger a workflow_dispatch event."""
        payload: dict = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        resp = self.session.post(
            f"{self.base}/repos/{repo}/actions/workflows/{workflow_id}/dispatches",
            json=payload,
        )
        resp.raise_for_status()
        return f"Workflow '{workflow_id}' triggered on {ref}"

    def list_workflows(self, repo: str) -> list[dict]:
        """List available workflows in a repo."""
        data = self._get(f"/repos/{repo}/actions/workflows")
        return [
            {"id": w["id"], "name": w["name"], "path": w["path"], "state": w["state"]}
            for w in data.get("workflows", [])
        ]

    def add_issue_comment(self, repo: str, number: int, body: str) -> dict:
        """Add a comment to an issue or PR."""
        data = self._post(f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        return {"id": data["id"], "url": data["html_url"]}

    def get_pr_diff(self, repo: str, number: int) -> str:
        """Get the diff of a pull request."""
        resp = self.session.get(
            f"{self.base}/repos/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        resp.raise_for_status()
        return resp.text[:15000]  # cap at 15k chars


# ── Tool definitions for Claude's tool_use API ──────────────────────

GITHUB_TOOLS = [
    {
        "name": "get_file",
        "description": "Read the contents of a file from the GitHub repository. Use this to understand existing code before making changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root (e.g. 'src/main.py')"},
                "ref": {"type": "string", "description": "Branch or commit SHA. Omit for default branch."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories at a given path in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (empty string for repo root)", "default": ""},
                "ref": {"type": "string", "description": "Branch or commit SHA. Omit for default branch."},
            },
            "required": [],
        },
    },
    {
        "name": "create_or_update_file",
        "description": "Create a new file or update an existing file in the repository. For updates, the file's current SHA is fetched automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "The full file content to write"},
                "message": {"type": "string", "description": "Commit message"},
                "branch": {"type": "string", "description": "Branch to commit to"},
            },
            "required": ["path", "content", "message", "branch"],
        },
    },
    {
        "name": "create_branch",
        "description": "Create a new branch from an existing branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "Name of the new branch"},
                "from_branch": {"type": "string", "description": "Base branch (defaults to 'main')", "default": "main"},
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description"},
                "head": {"type": "string", "description": "Branch with changes"},
                "base": {"type": "string", "description": "Target branch (defaults to 'main')", "default": "main"},
            },
            "required": ["title", "body", "head"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues on the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "get_issue",
        "description": "Get details of a specific issue by number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Issue number"},
            },
            "required": ["number"],
        },
    },
    {
        "name": "list_pull_requests",
        "description": "List pull requests on the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "search_code",
        "description": "Search for code in the repository by keyword or pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_branches",
        "description": "List all branches in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_default_branch",
        "description": "Get the name of the repository's default branch.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file from the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"},
                "message": {"type": "string", "description": "Commit message"},
                "branch": {"type": "string", "description": "Branch to delete from"},
            },
            "required": ["path", "message", "branch"],
        },
    },
    {
        "name": "get_tree",
        "description": "Get the full file tree of the repository. Fast way to see all files and their sizes without browsing directory by directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Branch or commit SHA (defaults to HEAD)"},
            },
            "required": [],
        },
    },
    {
        "name": "list_workflows",
        "description": "List available GitHub Actions workflows in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_workflow_runs",
        "description": "List recent GitHub Actions workflow runs (CI/CD). Check if tests/builds are passing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Filter by branch name"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": [],
        },
    },
    {
        "name": "get_workflow_run",
        "description": "Get details of a specific workflow run by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer", "description": "The workflow run ID"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_workflow_run_logs",
        "description": "Get the failed step details from a workflow run. Use this to diagnose CI failures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer", "description": "The workflow run ID"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "trigger_workflow",
        "description": "Trigger a GitHub Actions workflow (must support workflow_dispatch). Use this to run tests or CI after making changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string", "description": "Workflow filename (e.g. 'ci.yml') or ID"},
                "ref": {"type": "string", "description": "Branch to run on (defaults to main)", "default": "main"},
            },
            "required": ["workflow_id"],
        },
    },
    {
        "name": "add_issue_comment",
        "description": "Add a comment to an issue or pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Issue or PR number"},
                "body": {"type": "string", "description": "Comment body (markdown)"},
            },
            "required": ["number", "body"],
        },
    },
    {
        "name": "get_pr_diff",
        "description": "Get the diff of a pull request. Use this to review changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "PR number"},
            },
            "required": ["number"],
        },
    },
]


def execute_tool(gh: GitHubClient, repo: str, tool_name: str, tool_input: dict) -> str:
    """Execute a GitHub tool call and return the result as a string."""
    import json

    try:
        if tool_name == "get_file":
            result = gh.get_file(repo, tool_input["path"], tool_input.get("ref"))
        elif tool_name == "list_directory":
            result = gh.list_directory(repo, tool_input.get("path", ""), tool_input.get("ref"))
        elif tool_name == "create_or_update_file":
            # Auto-fetch SHA for updates
            sha = gh.get_file_sha(repo, tool_input["path"], ref=tool_input["branch"])
            result = gh.create_or_update_file(
                repo,
                tool_input["path"],
                tool_input["content"],
                tool_input["message"],
                tool_input["branch"],
                sha=sha,
            )
            result = f"File {'updated' if sha else 'created'}: {tool_input['path']} on {tool_input['branch']}"
        elif tool_name == "create_branch":
            gh.create_branch(repo, tool_input["branch_name"], tool_input.get("from_branch", "main"))
            result = f"Branch '{tool_input['branch_name']}' created from '{tool_input.get('from_branch', 'main')}'"
        elif tool_name == "create_pull_request":
            result = gh.create_pull_request(
                repo,
                tool_input["title"],
                tool_input["body"],
                tool_input["head"],
                tool_input.get("base", "main"),
            )
        elif tool_name == "list_issues":
            result = gh.list_issues(repo, tool_input.get("state", "open"), tool_input.get("limit", 10))
        elif tool_name == "get_issue":
            result = gh.get_issue(repo, tool_input["number"])
        elif tool_name == "list_pull_requests":
            result = gh.list_pull_requests(repo, tool_input.get("state", "open"), tool_input.get("limit", 10))
        elif tool_name == "search_code":
            result = gh.search_code(repo, tool_input["query"])
        elif tool_name == "list_branches":
            result = gh.list_branches(repo)
        elif tool_name == "get_default_branch":
            result = gh.get_default_branch(repo)
        elif tool_name == "delete_file":
            result = gh.delete_file(repo, tool_input["path"], tool_input["message"], tool_input["branch"])
        elif tool_name == "get_tree":
            result = gh.get_tree(repo, tool_input.get("ref", "HEAD"))
        elif tool_name == "list_workflows":
            result = gh.list_workflows(repo)
        elif tool_name == "list_workflow_runs":
            result = gh.list_workflow_runs(repo, tool_input.get("branch"), tool_input.get("limit", 5))
        elif tool_name == "get_workflow_run":
            result = gh.get_workflow_run(repo, tool_input["run_id"])
        elif tool_name == "get_workflow_run_logs":
            result = gh.get_workflow_run_logs(repo, tool_input["run_id"])
        elif tool_name == "trigger_workflow":
            result = gh.trigger_workflow(repo, tool_input["workflow_id"], tool_input.get("ref", "main"))
        elif tool_name == "add_issue_comment":
            result = gh.add_issue_comment(repo, tool_input["number"], tool_input["body"])
        elif tool_name == "get_pr_diff":
            result = gh.get_pr_diff(repo, tool_input["number"])
        else:
            result = f"Unknown tool: {tool_name}"

        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)

    except requests.HTTPError as e:
        return f"GitHub API error ({e.response.status_code}): {e.response.text[:500]}"
    except Exception as e:
        return f"Error: {e}"
