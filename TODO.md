# Teleclaude — To-Do List

Last updated: 2026-02-14

Competitive reference: [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) (~350 stars) — more mature engineering practices, narrower scope. Single-user bot, so per-user budgets and rate limiting are not priorities.

---

## Tier 1 — Fix What's Broken

### 1. GitHub token exposed in git clone URLs
- **File:** `claude_code.py:41`
- **Issue:** `url = f"https://{self.github_token}@github.com/{repo}.git"` exposes the token in process lists, shell history, and logs.
- **Fix:** Use `gh repo clone` or SSH keys instead of embedding tokens in URLs.

### 2. Directory sandboxing for Agent bot
- **File:** `bot_agent.py`, `claude_code.py`
- **Issue:** Agent bot has open file access with no path traversal prevention. Competitor uses `APPROVED_DIRECTORY` + path validation.
- **Fix:** Restrict Agent bot operations to an approved directory. Validate all paths to prevent traversal (`../` etc).

### 3. Request timeouts on all external API calls
- **Files:** `github_tools.py`, `web_tools.py`, `calendar_tools.py`, `tasks_tools.py`, `email_tools.py`
- **Issue:** HTTP requests (especially GitHub API via `requests.Session`) have no explicit timeouts. Can hang indefinitely on network issues.
- **Fix:** Add `timeout=30` (or similar) to all `requests` calls and Google API calls.

### 4. Deprecated `datetime.utcnow()`
- **File:** `calendar_tools.py:31`
- **Issue:** `datetime.utcnow()` is deprecated in Python 3.12+ and will break on upgrade.
- **Fix:** Replace with `datetime.now(datetime.UTC)`.

---

## Tier 2 — Engineering Quality

### 5. Test coverage enforcement
- **Current state:** Tests exist for helpers, persistence, GitHub tools, web tools, and ClaudeCodeManager. But **zero tests** for:
  - `bot.py` message handlers (core logic)
  - `bot_agent.py` (streaming, CLI integration)
  - Google integrations (`calendar_tools.py`, `tasks_tools.py`, `email_tools.py`)
  - Async behavior
- **Competitor:** Enforces >85% coverage.
- **Fix:** Add tests for the above. Add `--cov-fail-under=80` to pytest config in `pyproject.toml`. Update CI to enforce.

### 6. Audit logging
- **Issue:** No structured logging of user actions. Makes debugging production issues harder.
- **Fix:** Log all tool invocations, errors, and key user actions to the SQLite database or a structured log file. Useful for debugging even as a single user.

### 7. Update anthropic dependency
- **File:** `requirements.txt`
- **Issue:** `anthropic>=0.49.0` is outdated. Current versions have bug fixes and new features.
- **Fix:** Update to latest version, test for breaking changes.

---

## Tier 3 — Features & Polish

### 8. Inbound GitHub webhooks
- **Issue:** Teleclaude has 20+ GitHub tools for outbound actions but no event-driven triggers. Competitor has webhook support with HMAC-SHA256 verification.
- **Fix:** Add a webhook endpoint (Flask/aiohttp) that receives GitHub push, PR, and issue events. Notify via Telegram when relevant events occur. Would complete the GitHub integration story.

### 9. Fix README deployment documentation
- **File:** `README.md`
- **Issue:** README mentions Railway as deployment target, but `deploy.yml` and `deploy.sh` target DigitalOcean.
- **Fix:** Update README to reflect actual deployment setup.

### 10. Dependency security scanning
- **Issue:** No automated scanning for vulnerable dependencies. No Dependabot, pip-audit, or similar.
- **Fix:** Add `pip-audit` to CI pipeline and/or enable Dependabot on the GitHub repo.

---

## Context from Competitive Analysis

**Where Teleclaude wins:** 20+ native GitHub tools, Google Calendar/Tasks/Gmail integration, web search, model switching, plan mode, conversation history management.

**Where competitor wins:** Test coverage (>85% enforced), rate limiting (token bucket), per-user cost caps, GitHub webhooks, directory sandboxing, audit logging, classic terminal mode.

**Strategy:** Close the engineering quality gap (Tiers 1-2) while maintaining the feature breadth advantage. The competitor is narrower in scope — Teleclaude doesn't need to copy everything, just shore up the production-readiness gaps.
