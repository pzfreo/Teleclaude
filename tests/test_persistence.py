"""Tests for persistence.py — SQLite storage layer."""

from unittest.mock import patch


class TestInitDb:
    def test_creates_tables(self, tmp_db):
        import sqlite3

        conn = sqlite3.connect(str(tmp_db))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "conversations" in tables
        assert "active_repos" in tables
        assert "todo_lists" in tables
        assert "chat_modes" in tables

    def test_idempotent(self, tmp_db):
        """Calling init_db twice should not error."""
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import init_db

            init_db()  # second call — should not raise


class TestConversations:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_conversation, save_conversation

            msgs = [{"role": "user", "content": "hello"}]
            save_conversation(1001, msgs)
            loaded = load_conversation(1001)
            assert loaded == msgs

    def test_load_empty(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_conversation

            assert load_conversation(9999) == []

    def test_clear(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import clear_conversation, load_conversation, save_conversation

            save_conversation(1001, [{"role": "user", "content": "hi"}])
            clear_conversation(1001)
            assert load_conversation(1001) == []

    def test_overwrite(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_conversation, save_conversation

            save_conversation(1001, [{"role": "user", "content": "first"}])
            save_conversation(1001, [{"role": "user", "content": "second"}])
            loaded = load_conversation(1001)
            assert len(loaded) == 1
            assert loaded[0]["content"] == "second"


class TestActiveRepo:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_active_repo, save_active_repo

            save_active_repo(1001, "owner/repo")
            assert load_active_repo(1001) == "owner/repo"

    def test_load_none(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_active_repo

            assert load_active_repo(9999) is None


class TestActiveBranch:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_active_branch, save_active_branch, save_active_repo

            save_active_repo(1001, "owner/repo")  # branch requires repo row
            save_active_branch(1001, "feature-x")
            assert load_active_branch(1001) == "feature-x"

    def test_clear_branch(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_active_branch, save_active_branch, save_active_repo

            save_active_repo(1001, "owner/repo")
            save_active_branch(1001, "feature-x")
            save_active_branch(1001, None)
            assert load_active_branch(1001) is None


class TestTodos:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_todos, save_todos

            todos = [{"content": "test task", "status": "pending"}]
            save_todos(1001, todos)
            assert load_todos(1001) == todos

    def test_load_empty(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_todos

            assert load_todos(9999) == []


class TestPlanMode:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_plan_mode, save_plan_mode

            save_plan_mode(1001, True)
            assert load_plan_mode(1001) is True

    def test_default_false(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_plan_mode

            assert load_plan_mode(9999) is False


class TestAgentMode:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_agent_mode, save_agent_mode

            save_agent_mode(1001, True)
            assert load_agent_mode(1001) is True

    def test_default_false(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_agent_mode

            assert load_agent_mode(9999) is False


class TestModel:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_model, save_model

            save_model(1001, "claude-opus-4-6")
            assert load_model(1001) == "claude-opus-4-6"

    def test_load_none(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_model

            assert load_model(9999) is None


class TestSerialize:
    def test_model_dump(self, tmp_db):
        """Objects with model_dump() should be serialized via that method."""
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_conversation, save_conversation

            class FakeBlock:
                def model_dump(self):
                    return {"type": "text", "text": "hello"}

            msgs = [{"role": "assistant", "content": [FakeBlock()]}]
            save_conversation(1001, msgs)
            loaded = load_conversation(1001)
            assert loaded[0]["content"][0] == {"type": "text", "text": "hello"}

    def test_dict_fallback(self, tmp_db):
        """Objects without model_dump should fall back to __dict__."""
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_conversation, save_conversation

            class SimpleObj:
                def __init__(self):
                    self.key = "value"

            msgs = [{"role": "user", "content": SimpleObj()}]
            save_conversation(1001, msgs)
            loaded = load_conversation(1001)
            assert loaded[0]["content"]["key"] == "value"
