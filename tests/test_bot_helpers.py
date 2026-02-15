"""Tests for helper functions in bot.py."""


class TestTrimContent:
    """Tests for _trim_content()."""

    def test_short_string_unchanged(self):
        from bot import _trim_content

        assert _trim_content("short text") == "short text"

    def test_long_string_truncated(self):
        from bot import MAX_CONTENT_SIZE, _trim_content

        long_text = "x" * (MAX_CONTENT_SIZE + 100)
        result = _trim_content(long_text)
        assert len(result) < len(long_text)
        assert result.endswith("... (truncated)")

    def test_list_with_images_kept(self):
        from bot import _trim_content

        content = [
            {"type": "image", "source": {"type": "base64", "data": "abc123"}},
            {"type": "text", "text": "hello"},
        ]
        result = _trim_content(content, keep_images=True)
        assert result[0]["type"] == "image"

    def test_list_images_stripped(self):
        from bot import _trim_content

        content = [
            {"type": "image", "source": {"type": "base64", "data": "abc123"}},
            {"type": "text", "text": "hello"},
        ]
        result = _trim_content(content, keep_images=False)
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "[image was here]"

    def test_list_documents_stripped(self):
        from bot import _trim_content

        content = [
            {"type": "document", "source": {"type": "base64", "data": "pdf123"}},
        ]
        result = _trim_content(content, keep_images=False)
        assert result[0]["text"] == "[document was here]"

    def test_non_list_non_string_passthrough(self):
        from bot import _trim_content

        assert _trim_content(42) == 42
        assert _trim_content(None) is None

    def test_truncates_nested_content_field(self):
        from bot import MAX_CONTENT_SIZE, _trim_content

        content = [
            {"type": "tool_result", "content": "x" * (MAX_CONTENT_SIZE + 100)},
        ]
        result = _trim_content(content)
        assert result[0]["content"].endswith("... (truncated)")
        assert len(result[0]["content"]) < MAX_CONTENT_SIZE + 50


class TestSanitizeHistory:
    """Tests for _sanitize_history()."""

    def test_empty_history(self):
        from bot import _sanitize_history

        assert _sanitize_history([]) == []

    def test_simple_conversation(self):
        from bot import _sanitize_history

        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = _sanitize_history(history)
        assert len(result) == 2

    def test_removes_leading_assistant_messages(self):
        from bot import _sanitize_history

        history = [
            {"role": "assistant", "content": "stale"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = _sanitize_history(history)
        assert result[0]["role"] == "user"
        assert len(result) == 2

    def test_strips_thinking_blocks(self):
        from bot import _sanitize_history

        history = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me think..."},
                    {"type": "text", "text": "hi"},
                ],
            },
        ]
        result = _sanitize_history(history)
        assistant_content = result[1]["content"]
        assert all(b.get("type") != "thinking" for b in assistant_content)

    def test_keeps_complete_tool_pairs(self):
        from bot import _sanitize_history

        history = [
            {"role": "user", "content": "search for X"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool_1", "name": "web_search", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_1", "content": "result"},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Here's what I found"},
                ],
            },
        ]
        result = _sanitize_history(history)
        assert len(result) == 4

    def test_drops_orphaned_tool_use(self):
        from bot import _sanitize_history

        history = [
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool_1", "name": "web_search", "input": {}},
                ],
            },
            # Missing tool_result — next message is a new user message
            {"role": "user", "content": "never mind"},
        ]
        result = _sanitize_history(history)
        # The orphaned tool_use assistant message should be dropped
        roles = [m["role"] for m in result]
        assert roles == ["user", "user"]

    def test_drops_mismatched_tool_ids(self):
        from bot import _sanitize_history

        history = [
            {"role": "user", "content": "search"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool_1", "name": "web_search", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_WRONG", "content": "result"},
                ],
            },
        ]
        result = _sanitize_history(history)
        # The orphaned tool_use (assistant) is dropped; both user messages remain
        assert len(result) == 2
        assert result[0]["content"] == "search"
        # The tool_result user message is kept (it's still a valid user message)
        assert result[1]["role"] == "user"


class TestFormatTodoList:
    def test_empty(self):
        from bot import format_todo_list

        assert format_todo_list([]) == "No tasks tracked."

    def test_formatting(self):
        from bot import format_todo_list

        todos = [
            {"content": "Task A", "status": "pending"},
            {"content": "Task B", "status": "in_progress"},
            {"content": "Task C", "status": "completed"},
        ]
        result = format_todo_list(todos)
        assert "[ ] 1. Task A" in result
        assert "[~] 2. Task B" in result
        assert "[x] 3. Task C" in result

    def test_unknown_status_defaults_to_pending(self):
        from bot import format_todo_list

        todos = [{"content": "Task", "status": "unknown_status"}]
        result = format_todo_list(todos)
        assert "[ ] 1. Task" in result

    def test_missing_status_defaults_to_pending(self):
        from bot import format_todo_list

        todos = [{"content": "Task"}]
        result = format_todo_list(todos)
        assert "[ ] 1. Task" in result


class TestIsAuthorized:
    def test_empty_allowlist_allows_all(self):
        from bot import ALLOWED_USER_IDS, is_authorized

        original = ALLOWED_USER_IDS.copy()
        ALLOWED_USER_IDS.clear()
        try:
            assert is_authorized(99999) is True
        finally:
            ALLOWED_USER_IDS.update(original)

    def test_allowlist_blocks_unknown(self):
        from bot import ALLOWED_USER_IDS, is_authorized

        original = ALLOWED_USER_IDS.copy()
        ALLOWED_USER_IDS.clear()
        ALLOWED_USER_IDS.add(12345)
        try:
            assert is_authorized(12345) is True
            assert is_authorized(99999) is False
        finally:
            ALLOWED_USER_IDS.clear()
            ALLOWED_USER_IDS.update(original)


class TestExtendedThinking:
    """Tests for _wants_extended_thinking()."""

    def test_think_about(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("think about this problem") is True

    def test_think_through(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("think through the architecture") is True

    def test_think_deeply(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("think deeply about this") is True

    def test_step_by_step(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("think step by step") is True

    def test_reason_through(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("reason through this") is True

    def test_reason_carefully(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("reason carefully about X") is True

    def test_analyze_carefully(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("analyze carefully this code") is True

    def test_case_insensitive(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("THINK ABOUT this") is True

    def test_false_positive_i_think_so(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("I think so") is False

    def test_false_positive_what_do_you_think(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("what do you think?") is False

    def test_false_positive_thinking(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("I was thinking about lunch") is False

    def test_plain_message(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("hello world") is False

    def test_multimodal_content(self):
        from bot import _wants_extended_thinking

        content = [
            {"type": "image", "source": {"type": "base64", "data": "abc"}},
            {"type": "text", "text": "think about this image"},
        ]
        assert _wants_extended_thinking(content) is True

    def test_multimodal_no_match(self):
        from bot import _wants_extended_thinking

        content = [
            {"type": "image", "source": {"type": "base64", "data": "abc"}},
            {"type": "text", "text": "what is this?"},
        ]
        assert _wants_extended_thinking(content) is False

    def test_empty_string(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking("") is False

    def test_empty_list(self):
        from bot import _wants_extended_thinking

        assert _wants_extended_thinking([]) is False
