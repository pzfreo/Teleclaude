.PHONY: format lint typecheck test test-cov check install install-dev clean

# Format code with Black and sort imports with ruff
format:
	uv run black .
	uv run ruff check --fix .

# Lint with ruff (no auto-fix)
lint:
	uv run ruff check .

# Type check with mypy
typecheck:
	uv run mypy bot.py bot_agent.py persistence.py shared.py github_tools.py web_tools.py \
	     claude_code.py calendar_tools.py tasks_tools.py email_tools.py webhooks.py \
	     streaming.py mcp_tools.py

# Run tests
test:
	uv run pytest

# Run tests with coverage
test-cov:
	uv run pytest --cov --cov-report=term-missing

# Run all checks (CI equivalent)
check: lint typecheck test

# Install runtime deps
install:
	uv sync --no-dev

# Install dev deps
install-dev:
	uv sync

# Clean caches
clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov .venv
