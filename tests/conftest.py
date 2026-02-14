"""Shared test fixtures for Teleclaude tests."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary SQLite database for persistence tests."""
    db_path = tmp_path / "test.db"
    with patch("persistence.DB_PATH", db_path):
        from persistence import init_db

        init_db()
        yield db_path


@pytest.fixture
def mock_github_session():
    """Mock requests.Session for GitHub API tests."""
    session = MagicMock()

    def make_response(json_data, status_code=200):
        resp = MagicMock()
        resp.json.return_value = json_data
        resp.status_code = status_code
        resp.text = json.dumps(json_data) if isinstance(json_data, (dict, list)) else str(json_data)
        resp.raise_for_status.return_value = None
        return resp

    session._make_response = make_response
    return session


@pytest.fixture
def github_client(mock_github_session):
    """GitHubClient with mocked HTTP session."""
    from github_tools import GitHubClient

    client = GitHubClient("fake-token")
    client.session = mock_github_session
    return client


@pytest.fixture
def mock_telegram_bot():
    """Mocked Telegram Bot object."""
    bot = AsyncMock()
    bot.get_file = AsyncMock()
    bot.send_message = AsyncMock()
    return bot
