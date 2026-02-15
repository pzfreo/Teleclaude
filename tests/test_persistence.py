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


class TestSessionId:
    def test_save_and_load(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_session_id, save_active_repo, save_session_id

            save_active_repo(1001, "owner/repo")  # session_id requires repo row
            save_session_id(1001, "session-abc-123")
            assert load_session_id(1001) == "session-abc-123"

    def test_load_none_when_no_repo(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_session_id

            assert load_session_id(9999) is None

    def test_clear_session(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_session_id, save_active_repo, save_session_id

            save_active_repo(1001, "owner/repo")
            save_session_id(1001, "session-abc-123")
            save_session_id(1001, None)
            assert load_session_id(1001) is None

    def test_overwrite(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_session_id, save_active_repo, save_session_id

            save_active_repo(1001, "owner/repo")
            save_session_id(1001, "old-session")
            save_session_id(1001, "new-session")
            assert load_session_id(1001) == "new-session"


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


class TestAuditLog:
    """TODO #6: Verify audit log writes and reads correctly."""

    def test_write_and_read(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import audit_log, get_audit_log

            audit_log("tool_call", chat_id=1001, user_id=42, detail="web_search: test query")
            entries = get_audit_log(limit=10)
            assert len(entries) == 1
            assert entries[0]["event"] == "tool_call"
            assert entries[0]["chat_id"] == 1001
            assert entries[0]["user_id"] == 42
            assert entries[0]["detail"] == "web_search: test query"

    def test_read_empty(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import get_audit_log

            assert get_audit_log() == []

    def test_filter_by_chat_id(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import audit_log, get_audit_log

            audit_log("event_a", chat_id=1001, detail="a")
            audit_log("event_b", chat_id=2002, detail="b")
            audit_log("event_c", chat_id=1001, detail="c")

            entries = get_audit_log(chat_id=1001)
            assert len(entries) == 2
            assert all(e["chat_id"] == 1001 for e in entries)

    def test_limit_respected(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import audit_log, get_audit_log

            for i in range(10):
                audit_log("event", chat_id=1, detail=f"entry_{i}")

            entries = get_audit_log(limit=3)
            assert len(entries) == 3

    def test_ordering_most_recent_first(self, tmp_db):
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import audit_log, get_audit_log

            audit_log("first", chat_id=1, detail="1")
            audit_log("second", chat_id=1, detail="2")
            audit_log("third", chat_id=1, detail="3")

            entries = get_audit_log(limit=10)
            assert entries[0]["event"] == "third"
            assert entries[2]["event"] == "first"

    def test_no_token_in_detail(self, tmp_db):
        """Audit log should not store sensitive tokens."""
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import audit_log, get_audit_log

            # Simulate a tool call detail — should not contain tokens
            audit_log("tool_call", chat_id=1, detail="get_file on owner/repo")
            entries = get_audit_log()
            # Verify the detail field doesn't contain common token patterns
            for entry in entries:
                assert "ghp_" not in entry["detail"]
                assert "sk-ant-" not in entry["detail"]

    def test_write_failure_does_not_raise(self, tmp_db):
        """audit_log silently catches errors."""
        with patch("persistence._connect", side_effect=RuntimeError("DB locked")):
            from persistence import audit_log

            # Should not raise
            audit_log("event", chat_id=1, detail="test")


class TestSerialize:
    def test_model_dump(self, tmp_db):
        """Objects with model_dump() should be serialized via that method."""
        with patch("persistence.DB_PATH", tmp_db):
            from persistence import load_conversation, save_conversation

            class FakeBlock:
                def model_dump(self, **kwargs):
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
