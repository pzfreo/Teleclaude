.PHONY: format lint typecheck test test-cov check install install-dev clean

# Format code with Black and sort imports with ruff
format:
	black .
	ruff check --fix .

# Lint with ruff (no auto-fix)
lint:
	ruff check .

# Type check with mypy
typecheck:
	mypy bot.py bot_agent.py persistence.py shared.py github_tools.py web_tools.py \
	     claude_code.py calendar_tools.py tasks_tools.py email_tools.py

# Run tests
test:
	pytest

# Run tests with coverage
test-cov:
	pytest --cov --cov-report=term-missing

# Run all checks (CI equivalent)
check: lint typecheck test

# Install runtime deps
install:
	pip install -e .

# Install dev deps
install-dev:
	pip install -e ".[dev]"

# Clean caches
clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache .ruff_cache .coverage htmlcov
