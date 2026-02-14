# Teleclaude — To-Do List

Last updated: 2026-02-14

Competitive references:
- [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) (~350 stars) — Mature engineering, narrower scope, single-user CLI wrapper.
- [linuz90/claude-telegram-bot](https://github.com/linuz90/claude-telegram-bot) (~364 stars) — TypeScript/Bun, Claude Agent SDK, MCP-based extensibility. Different philosophy: streaming-first, MCP plugins, macOS-focused.

---

## Quality Gate — All Changes Must Meet

Every item in this TODO must satisfy these standards before merging to `main`. The project uses `pyproject.toml` for all tooling config.

### Testing requirements
- **New code must have tests.** Every new module, function, or feature branch must include tests in `tests/`.
- **Follow existing patterns.** Tests use `pytest` + `pytest-asyncio` (auto mode). Fixtures live in `tests/conftest.py`. Mock external APIs with `unittest.mock` — never call real Telegram, Anthropic, Google, or GitHub APIs in tests.
- **Coverage floor:** `fail_under = 55` in `pyproject.toml` (current: 59%). Target raising to 70 after Tier 3 items land, then 80 long-term.
- **New features must not lower coverage.** If adding a new module (e.g., `streaming.py`), include a corresponding `tests/test_streaming.py`.
- **Async tests:** Use `async def test_*` — `pytest-asyncio` with `asyncio_mode = "auto"` handles the event loop. Use `AsyncMock` for Telegram bot methods and Anthropic client calls.

### Code quality
- **Formatting:** `black --check .` (line-length 120, target py312)
- **Linting:** `ruff check .` — rules: E, W, F, I, UP, B, SIM, RUF, C4, PIE. See `pyproject.toml [tool.ruff.lint]` for ignored rules.
- **Type checking:** `mypy` on all core modules. New modules must be added to the CI mypy command in `.github/workflows/ci.yml`.
- **Security:** `pip-audit` in CI. New dependencies must not introduce known vulnerabilities.
- **No new ignores:** Don't add `# type: ignore`, `# noqa`, or ruff `ignore` entries without a comment explaining why.

### PR checklist (for each item below)
1. [ ] Implementation complete
2. [ ] Tests written and passing (`pytest --cov --cov-report=term-missing`)
3. [ ] Black + Ruff + mypy pass
4. [ ] New module added to mypy CI command if applicable
5. [ ] `pyproject.toml` updated if new dependencies added
6. [ ] No secrets in code (tokens, keys — use env vars)
7. [ ] Manual smoke test on Telegram (for UX changes)

---

## Tier 1 — Fix What's Broken (all resolved)

### 1. ✅ GitHub token no longer in clone URLs
- **File:** `claude_code.py`
- **Status:** Fixed. `ensure_clone()` uses `https://github.com/{repo}.git` (no token). `_git_env()` passes credentials via `GIT_CONFIG` credential helper.
- **Tests:** `tests/test_claude_code.py::TestTokenNotInCloneUrl` — verifies clone URL is token-free, credential helper is configured, empty token skips config.

### 2. ✅ Directory sandboxing implemented
- **File:** `claude_code.py`
- **Status:** Fixed. `workspace_path()` resolves paths and checks they're within the workspace root. Raises `ValueError` on traversal attempts.
- **Tests:** `tests/test_claude_code.py::TestPathTraversal` — tests `../` in owner, `../../` in repo name, absolute path escape, valid repos pass.

### 3. ✅ Request timeouts on all external API calls
- **Files:** `github_tools.py` (`DEFAULT_TIMEOUT = 30`), `web_tools.py` (`DDGS(timeout=30)`), `calendar_tools.py` / `tasks_tools.py` / `email_tools.py` (`Request(timeout=30)`)
- **Status:** Fixed. All HTTP clients use explicit timeouts.
- **Tests:** `tests/test_github_tools.py::TestTimeouts` — verifies timeout passed to GET/POST/PUT/PATCH/DELETE, `Timeout` exception handled gracefully.

### 4. ✅ Deprecated `datetime.utcnow()` removed
- **Status:** Fixed. All code uses `datetime.now(tz=timezone.utc)`. Ruff rule `UP017` (included in `UP` ruleset) prevents regression.
- **Tests:** Covered by existing `tests/test_calendar_tools.py`. Zero `utcnow()` calls in codebase (verified by grep).

---

## Tier 2 — Engineering Quality (all resolved)

### 5. ✅ Test coverage enforcement
- **Status:** Implemented. Coverage raised from 42% to 59%. `fail_under` raised from 40 → 55.
- **New tests added:**
  - `tests/test_bot_handlers.py` (66 tests) — `_call_anthropic` retry logic, `_execute_tool_call` dispatch (7 tool routing tests), cache functions (8 tests), command handlers (11 tests), `handle_message` entry point (4 tests), `_process_message` loop (5 tests including tool use loop, truncation, API error rollback, max rounds), `_build_user_content` (6 tests), `send_long_message` (3 tests), `trim_history` (2 tests), `keep_typing` (1 test)
  - `tests/test_bot_agent.py` expanded (15 new tests) — `_run_cli` (5 tests: no repo, success, empty result, CLI error, conversation save), `handle_message` (5 tests: auth, empty, text, noop, voice), command handlers (4 tests: start, new, model, version), `keep_typing` (1 test)
  - Google tools verified: `calendar_tools` 92%, `tasks_tools` 78%, `email_tools` 100%

### 6. ✅ Audit logging
- **Status:** Implemented. `persistence.py` has `audit_log()` and `get_audit_log()` backed by SQLite `audit_log` table. Called from `bot.py` tool dispatch.
- **Tests:** `tests/test_persistence.py::TestAuditLog` — write/read, filter by chat_id, limit, ordering (most recent first), no-token-in-detail check, silent failure on DB error.

### 7. ✅ Anthropic dependency verified current
- **Status:** `anthropic>=0.79.0` is already the latest available version (0.79.0). No update needed.
- **Tests:** Full test suite passes (212 tests) with current version.

---

## Tier 3 — UX Improvements (inspired by linuz90/claude-telegram-bot)

### 8. Streaming responses ⭐ HIGH IMPACT
- **Issue:** API bot buffers the full response before sending to Telegram. Users stare at "typing..." for 10-30 seconds with no feedback. This is the biggest UX gap vs competitors.
- **Current state:** Agent bot streams from CLI but accumulates the result. API bot uses `client.messages.create()` synchronously.
- **Approach:**
  1. Switch API bot to `client.messages.stream()` (Anthropic streaming API)
  2. Send initial Telegram message on first token, then `edit_message_text()` every ~1 second as tokens arrive
  3. Handle Telegram's rate limits (max 30 edits/sec per chat, practically ~1-2/sec)
  4. Handle markdown formatting mid-stream (partial code blocks, etc.)
  5. Fall back to full-response on edit failures
- **Complexity:** Medium — main challenges are Telegram rate limiting and partial markdown rendering.
- **Files:** `bot.py` (API call path), potentially new `streaming.py` helper
- **Tests:** Create `tests/test_streaming.py`:
  - Mock `client.messages.stream()` to yield token chunks. Verify accumulated text is correct.
  - Mock `bot.edit_message_text()` — verify it's called at throttled intervals (~1s), not per-token.
  - Test partial markdown safety: unclosed code block mid-stream should be temporarily closed before edit.
  - Test fallback: simulate `telegram.error.BadRequest` on edit → verify falls back to full-response send.
  - Test tool_use during stream: verify stream pauses, tool executes, stream resumes.
  - Add `streaming.py` to mypy CI command.

### 9. Inline keyboard buttons for user choices ⭐ HIGH IMPACT
- **Issue:** Confirmations and choices are currently text-based ("reply yes/no"). linuz90 has an `ask_user` MCP tool that renders tappable inline keyboard buttons — much better mobile UX.
- **Approach:**
  1. Add an Anthropic tool `ask_user` with parameters: `question` (string), `options` (list of strings)
  2. When Claude calls this tool, render an `InlineKeyboardMarkup` in Telegram
  3. Wait for user tap via `CallbackQueryHandler`
  4. Return selected option as tool result to continue the conversation
  5. Add timeout (e.g., 5 minutes) with fallback to text input
- **Use cases:** Repo selection, branch picking, confirm destructive actions, multiple-choice decisions
- **Complexity:** Medium — needs callback handler plumbing + async coordination between tool execution and user input.
- **Files:** `bot.py` (tool definition + dispatch + callback handler)
- **Tests:** Add to `tests/test_bot_helpers.py` or new `tests/test_ask_user.py`:
  - Test tool definition schema is valid (matches Anthropic tool_use format).
  - Test `InlineKeyboardMarkup` is built correctly from options list (1-5 buttons).
  - Test callback handler: simulate user tap → verify correct option string returned as tool result.
  - Test timeout: simulate no tap within 5 min → verify fallback message sent + tool returns timeout error.
  - Test edge case: user sends text message instead of tapping button → verify it's treated as freeform response.

### 10. Message queuing with interrupt
- **Issue:** Messages sent while Claude is processing are queued silently behind the per-chat lock. No way to interrupt a long-running request. linuz90 uses `!` prefix to interrupt.
- **Approach:**
  1. Track queued messages per chat (already implicit via `asyncio.Lock`)
  2. If a message starts with `!`, cancel the in-progress request and process the interrupt message instead
  3. Show queue status ("2 messages queued, processing...") in typing indicator
  4. On completion, auto-process next queued message
- **Complexity:** Medium — needs cancellation logic for in-flight API calls.
- **Files:** `bot.py` (message handler, lock management)
- **Tests:** Add `tests/test_message_queue.py`:
  - Test that messages arriving while locked are queued (not dropped).
  - Test `!`-prefixed message cancels in-progress task (mock the API call, verify it's cancelled).
  - Test queue ordering: messages processed FIFO after current request completes.
  - Test queue status reporting: verify typing indicator includes queue count.
  - Test edge case: `!` with empty queue (no in-progress task) treated as normal message.

### 11. Voice message transcription
- **Issue:** Voice messages and audio files are not supported. linuz90 uses OpenAI Whisper for transcription.
- **Approach:**
  1. Detect `message.voice` and `message.audio` in `_build_user_content()`
  2. Download the OGG/MP3 file from Telegram
  3. Transcribe via OpenAI Whisper API (`openai.audio.transcriptions.create()`) or local `whisper.cpp`
  4. Prepend "[Voice transcription]:" to the text content
  5. Optional: support video notes (`message.video_note`) too
- **Complexity:** Low — straightforward API call, similar to existing photo/document handling.
- **Dependencies:** `openai` package (for Whisper API) or `faster-whisper` (for local inference)
- **Files:** `bot.py` (`_build_user_content`), new config for `OPENAI_API_KEY`
- **Tests:** Add to `tests/test_bot_helpers.py`:
  - Mock `openai.audio.transcriptions.create()` → verify returned text is prepended with "[Voice transcription]:".
  - Test `_build_user_content()` with mocked `message.voice` object → verify audio bytes are downloaded and transcribed.
  - Test graceful fallback when `OPENAI_API_KEY` is not set → verify voice message returns helpful error, not crash.
  - Test unsupported audio format handling.

---

## Tier 4 — Architecture & Extensibility

### 12. MCP server support
- **Issue:** Tools are hardcoded Python modules. linuz90 uses MCP servers for plug-in extensibility (Notion, Things, Typefully, etc.).
- **Assessment:** Our deeply-integrated tools (GitHub 15+ ops, Google suite) are more capable than typical MCP servers. MCP would add extensibility without replacing existing tools.
- **Approach:**
  1. Add MCP client library to connect to external MCP servers
  2. Dynamically load tool definitions from configured MCP servers at startup
  3. Route tool calls to MCP servers alongside native tool dispatch
  4. Config: list of MCP server URLs/commands in `.env` or config file
- **Complexity:** High — requires MCP protocol implementation, dynamic tool registration, error handling for external processes.
- **Priority:** Low — nice to have, but our native tools are a stronger moat than plugin extensibility.
- **Tests:** Create `tests/test_mcp.py`:
  - Test MCP tool definition loading: mock MCP server → verify tools appear in Anthropic tool list.
  - Test tool dispatch routing: native tools route to native handlers, MCP tools route to MCP client.
  - Test MCP server failure: server unreachable at startup → verify bot starts without MCP tools (graceful degradation, like existing tool loading pattern).
  - Test MCP tool execution timeout: slow MCP server → verify timeout + error returned to LLM.
  - Add new `mcp.py` module to mypy CI command.

### 13. Extended thinking trigger keywords
- **Issue:** No way to trigger Claude's extended thinking from Telegram. linuz90 auto-enables it when user says "think".
- **Approach:**
  1. Detect keywords ("think about", "reason through", "analyze carefully") in user message
  2. Pass `thinking={"type": "enabled", "budget_tokens": N}` to Anthropic API
  3. Strip thinking blocks from response before sending to Telegram
- **Complexity:** Low
- **Files:** `bot.py` (`_call_anthropic`)
- **Tests:** Add to `tests/test_bot_helpers.py`:
  - Test keyword detection: "think about X" → thinking enabled; "I think so" → thinking NOT enabled (avoid false positives).
  - Test that thinking blocks are stripped from response content before sending to Telegram.
  - Test that `_call_anthropic` passes correct `thinking` parameter when triggered.

---

## Tier 5 — Existing Feature Improvements

### 14. Fix README deployment documentation
- **File:** `README.md`
- **Issue:** README mentions Railway as deployment target, but actual deployment is DigitalOcean.
- **Fix:** Update README to reflect actual deployment setup.
- **Tests:** N/A (documentation only). Verify links work manually.

### 15. Dependency security scanning
- **Issue:** No automated scanning for vulnerable dependencies.
- **Fix:** `pip-audit` is already in CI. Consider adding Dependabot config (`.github/dependabot.yml`).
- **Tests:** N/A (CI config only). Verify `pip-audit` exits 0 after dependency updates.

### 16. Inbound GitHub webhooks (enhancement)
- **Current state:** `webhooks.py` exists with basic webhook receiver and HMAC verification.
- **Enhancement:** Expand to handle more event types (PR reviews, CI status, deployments). Add configurable notification preferences.
- **Tests:** Expand `tests/test_webhooks.py` — add test cases for each new event type. Test notification filtering (e.g., user only wants PR events, not push events). Test malformed webhook payloads return 400.

---

## Competitive Positioning

### Where Teleclaude wins
- **Deep integrations:** 20+ native GitHub tools, Google Calendar/Tasks/Gmail, web search — far richer than MCP plugin equivalents
- **Server deployment:** Docker Compose, CI/CD, DigitalOcean — production-grade vs macOS-only
- **Conversation persistence:** SQLite with history management, crash recovery
- **Dual-bot architecture:** Clean separation of API bot (tools) and Agent bot (filesystem)
- **Plan mode:** Multi-step task planning with TODO tracking

### Where linuz90 wins
- **Streaming responses:** Real-time output vs wait-for-complete
- **MCP extensibility:** Plug-in architecture vs hardcoded modules
- **Voice support:** Whisper transcription for voice memos
- **Inline buttons:** Tappable choices vs text-based confirmations
- **Message queuing:** Explicit queue management with interrupt support

### Strategy
Close the UX gap (Tier 3) while maintaining the integration depth advantage. Streaming responses (#8) and inline buttons (#9) are the highest-ROI improvements. MCP (#12) is architecturally interesting but lower priority — our native tools are a stronger differentiator than plugin breadth.
