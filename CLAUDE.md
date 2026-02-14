# Teleclaude — Architecture & Development Guide

This document is for AI agents (Claude Code, Copilot, etc.) working on this codebase. It covers architecture, conventions, and quality standards so you can contribute without breaking things.

## Project overview

Teleclaude is a dual-bot Telegram system that connects users to Claude for coding, productivity, and daily tasks. It runs as two Docker containers sharing a common codebase.

- **Version:** See `VERSION` file (semver, currently 0.6.x)
- **Language:** Python 3.12+
- **Framework:** python-telegram-bot 21.6 (async)
- **AI backend:** Anthropic SDK (API bot) + Claude Code CLI (Agent bot)
- **Persistence:** SQLite (`data/teleclaude.db`)
- **Deployment:** Docker Compose on DigitalOcean, CI/CD via GitHub Actions

## Architecture: Dual Bot

### API Bot (`bot.py`, ~1400 lines)
The primary bot. Handles conversational AI with tool use via the Anthropic SDK directly.

- **Entry point:** `main()` → `Application.run_polling()`
- **Message flow:** `handle_message()` → `_build_user_content()` → `_process_message()`
- **AI call:** `_call_anthropic()` wraps `api_client.messages.create()` in a thread executor with retry logic (rate limits, overload). Currently **synchronous** — no streaming.
- **Tool loop:** Up to `MAX_TOOL_ROUNDS` (15) iterations. Each round: call API → if `stop_reason == "tool_use"` → dispatch tools → collect results → loop.
- **Tool dispatch:** `_execute_tool_call()` routes by tool name to the appropriate module handler.
- **Typing indicator:** Background `asyncio.Task` sends typing actions every 4s, progress messages after 15s.
- **Concurrency:** Per-chat `asyncio.Lock` prevents message interleaving. `concurrent_updates=True` on the Application.

### Agent Bot (`bot_agent.py`, ~670 lines)
Full-power filesystem access via Claude Code CLI subprocess.

- **Entry point:** `main()` → same Application pattern as API bot
- **AI call:** `ClaudeCodeManager.run()` launches `claude` CLI with `--output-format stream-json`
- **Streaming:** Reads JSON events from CLI stdout line-by-line. Calls `on_progress` callback for tool_use events. Accumulates result text.
- **Sessions:** Maintains CLI session IDs per chat for conversation continuity. `/new` clears the session.
- **Workspace:** Each repo gets a local clone under `workspaces/{owner}/{repo}/`

### Shared modules

| Module | Purpose | Key class/function |
|--------|---------|-------------------|
| `persistence.py` | SQLite CRUD for conversations, repos, todos, audit log | `init_db()`, `save_conversation()`, `audit_log()` |
| `shared.py` | Telegram helpers, auth, logging | `send_long_message()`, `is_authorized()`, `RingBufferHandler` |
| `claude_code.py` | CLI wrapper, workspace/session management | `ClaudeCodeManager` |
| `webhooks.py` | GitHub webhook receiver (aiohttp) | `create_webhook_app()`, `start_webhook_server()` |

### Tool modules

All tool modules follow the same pattern:

```python
# 1. Export tool definitions (Anthropic tool_use schema)
TOOL_NAME_TOOLS = [{"name": "...", "description": "...", "input_schema": {...}}]

# 2. Client class wrapping the external API
class SomeClient:
    def __init__(self, token_or_creds): ...

# 3. Dispatch function
def execute_tool(client: SomeClient, tool_name: str, tool_input: dict) -> str:
    if tool_name == "...":
        return json.dumps(client.some_method(...))
```

| Module | External API | Tools | Auth |
|--------|-------------|-------|------|
| `github_tools.py` | GitHub REST v3 | 15+ (files, branches, PRs, issues, code search) | Token |
| `web_tools.py` | DuckDuckGo (ddgs) | 1 (web_search) | None |
| `tasks_tools.py` | Google Tasks API | 5 (list, create, complete, update, delete) | OAuth2 |
| `calendar_tools.py` | Google Calendar API | 5 (list, create, update, delete events) | OAuth2 |
| `email_tools.py` | Gmail API (send only) | 1 (send_email) | OAuth2 |

Tool loading is **graceful** — each module is wrapped in try/except at import time (`bot.py:96-197`). Missing credentials or import failures disable the integration without crashing.

## Key patterns you must follow

### 1. In-memory cache backed by SQLite
State lives in module-level dicts (`conversations`, `active_repos`, `chat_models`, etc.) for fast access. SQLite is the backing store, written after each tool round for crash recovery.

```python
# Read: check cache first, fall back to DB
def get_conversation(chat_id):
    if chat_id not in conversations:
        conversations[chat_id] = _sanitize_history(load_conversation(chat_id))
    return conversations[chat_id]

# Write: update cache + persist
def save_state(chat_id):
    save_conversation(chat_id, get_conversation(chat_id))
```

### 2. History sanitization
The Anthropic API requires strict message ordering: `user` → `assistant` → `user` → ... Every `tool_use` block in an assistant message must have a matching `tool_result` in the next user message. `_sanitize_history()` enforces this by dropping orphaned pairs. If you add new message types to history, ensure they follow this contract.

### 3. Content size management
- Messages are split at 4096 chars (Telegram limit) via `send_long_message()`
- History is trimmed to `MAX_HISTORY * 2` messages
- Content blocks >20KB are truncated via `_trim_content()`
- Tool results >10KB are truncated
- Images are stripped from messages older than the last 10

### 4. Per-chat locking
Every message handler acquires `_chat_locks[chat_id]` before processing. This prevents concurrent API calls from corrupting conversation history. Never bypass this.

### 5. Error handling
- API retries: 3 attempts with exponential backoff for `RateLimitError` and `InternalServerError`
- On API error: history is rolled back to pre-request state
- Tool errors: caught per-tool, returned as error string to the LLM (not raised)
- Telegram errors: caught and logged, never crash the bot

## File layout

```
bot.py              # API bot entry point + message handlers + tool dispatch
bot_agent.py        # Agent bot entry point + CLI wrapper
claude_code.py      # ClaudeCodeManager (sessions, workspace, git)
persistence.py      # SQLite layer
shared.py           # Utilities (auth, Telegram helpers, logging)
github_tools.py     # GitHub REST API tools
web_tools.py        # Web search tool
calendar_tools.py   # Google Calendar tools
tasks_tools.py      # Google Tasks tools
email_tools.py      # Gmail send tool
webhooks.py         # GitHub webhook receiver
setup_google.py     # One-time OAuth2 setup script
pyproject.toml      # All tooling config (black, ruff, mypy, pytest, coverage)
VERSION             # Semver version string
Dockerfile          # Python 3.12-slim + Node.js 22 + Claude Code CLI
docker-compose.yml  # Two services: teleclaude (API bot) + teleclaude-agent
tests/              # pytest test suite
  conftest.py       # Shared fixtures (tmp_db, mock_github_session, etc.)
  test_*.py         # One test file per module
```

## Code quality standards

### Tooling (all configured in `pyproject.toml`)

| Tool | Command | Purpose |
|------|---------|---------|
| Black | `black --check .` | Formatting (line-length 120, py312) |
| Ruff | `ruff check .` | Linting (E, W, F, I, UP, B, SIM, RUF, C4, PIE) |
| mypy | `mypy bot.py bot_agent.py persistence.py shared.py github_tools.py web_tools.py claude_code.py calendar_tools.py tasks_tools.py email_tools.py webhooks.py` | Type checking |
| pytest | `pytest --cov --cov-report=term-missing` | Tests + coverage |
| pip-audit | `pip-audit` | Dependency vulnerability scan |

### Before committing
1. `black .` — format
2. `ruff check . --fix` — lint (auto-fix safe issues)
3. `mypy <modules>` — type check (add new modules to the list)
4. `pytest --cov` — tests must pass, coverage must not drop below `fail_under` in pyproject.toml

### Testing conventions
- **Framework:** pytest + pytest-asyncio (auto mode)
- **Fixtures:** `tests/conftest.py` — `tmp_db`, `mock_github_session`, `github_client`, `mock_telegram_bot`
- **Mocking:** Always mock external APIs (Telegram, Anthropic, Google, GitHub). Use `unittest.mock.AsyncMock` for async methods, `MagicMock` for sync.
- **File naming:** `tests/test_{module}.py` mirrors the source module
- **Test classes:** Group related tests in classes (`TestSomething`). No `setUp`/`tearDown` — use pytest fixtures.
- **Coverage floor:** `fail_under` in `pyproject.toml [tool.coverage.report]`. Currently 55, incrementally raising.

### Writing new tool modules
1. Follow the pattern in existing modules (export `TOOLS` list + `Client` class + `execute_tool` function)
2. Add graceful loading in `bot.py` (try/except at import, disable on failure)
3. Add tool names to dispatch in `_execute_tool_call()`
4. Add the module to `[tool.setuptools] py-modules` in `pyproject.toml`
5. Add the module to the mypy command in `.github/workflows/ci.yml`
6. Write tests in `tests/test_{module}.py`
7. Add any new dependencies to `pyproject.toml [project] dependencies`

### Style notes
- Line length: 120 (Black enforces)
- Imports: sorted by ruff (isort rules). First-party modules listed in `[tool.ruff.lint.isort]`
- Type hints: encouraged but not required (`disallow_untyped_defs = false`). Use `str | None` (not `Optional[str]`).
- Logging: use module-level `logger = logging.getLogger(__name__)`. Never `print()`.
- Env vars: access via `os.getenv()` with defaults. No hardcoded secrets.
- Telegram messages: keep concise — users are on phone screens.

## Deployment

### Docker
- `Dockerfile`: Python 3.12-slim base, Node.js 22 (for Claude CLI), GitHub CLI, non-root `teleclaude` user
- `docker-compose.yml`: Two services sharing the same image, different entrypoints and volumes
- Agent bot gets extra volumes: `workspaces/` (repo clones) and `.claude/` (CLI config)

### CI/CD (GitHub Actions)
- **CI** (`.github/workflows/ci.yml`): Runs on push to main + PRs. Black → Ruff → mypy → pip-audit → pytest.
- **Deploy** (`.github/workflows/deploy.yml`): Runs on push to main. rsync files to DigitalOcean droplet → docker compose up -d --build.

### Environment variables

**API bot (`.env`)**:
- `TELEGRAM_BOT_TOKEN` (required)
- `ANTHROPIC_API_KEY` (required)
- `GITHUB_TOKEN` (optional — enables GitHub tools)
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (optional — enables Tasks/Calendar/Gmail)
- `CLAUDE_MODEL` (default: claude-sonnet-4-5-20250929)
- `ALLOWED_USER_IDS` (comma-separated Telegram user IDs; empty = allow all)
- `TIMEZONE` (default: UTC)
- `DAILY_BRIEFING_TIME` (e.g., "08:00")

**Agent bot (`.env.agent`)**:
- `TELEGRAM_BOT_TOKEN` (required, different bot from API bot)
- `GITHUB_TOKEN` (required for repo cloning)
- `CLAUDE_MODEL` (default: claude-opus-4-6)
- `CLAUDE_CLI_PATH` (auto-detected if in PATH)
- `CLAUDE_CODE_WORKSPACE` (default: ./workspaces)
- `WEBHOOK_PORT` (0 = disabled)
- `GITHUB_WEBHOOK_SECRET` (for HMAC verification)

## Common pitfalls

1. **Don't modify history without sanitizing.** If you add/remove messages, call `_sanitize_history()` afterward or ensure tool_use/tool_result pairs are matched.

2. **Don't call external APIs in tests.** Every test must mock HTTP calls. The `conftest.py` fixtures provide mocked sessions — use them.

3. **Don't add modules without updating CI.** New `.py` modules need to be added to: `pyproject.toml [tool.setuptools] py-modules`, the mypy command in `ci.yml`, and have a corresponding test file.

4. **Don't bypass per-chat locks.** All message processing must go through the lock. Concurrent access to conversation history will corrupt it.

5. **Don't put tokens in URLs or logs.** Use credential helpers for git, env vars for everything else. The httpx logger is silenced specifically to prevent bot token leakage.

6. **Don't send long messages without splitting.** Always use `send_long_message()` for any text that might exceed 4096 chars.

7. **Don't assume integrations are available.** Check `gh_client`, `tasks_client`, etc. before using them. They may be `None` if credentials aren't configured.

8. **Tool results go to the LLM, not the user.** `_execute_tool_call()` returns a string that becomes a `tool_result` in the conversation. The LLM decides what to tell the user.

## Related docs

- `TODO.md` — Prioritized improvement backlog with test requirements per item
- `README.md` — User-facing setup and usage guide
- `pyproject.toml` — All tooling configuration (single source of truth)
