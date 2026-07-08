from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import bot_codex


class TestSendPathContainment:
    def test_allows_root_and_child(self, tmp_path):
        root = (tmp_path / "repo").resolve()
        child = (root / "out.txt").resolve()

        assert bot_codex._is_relative_to(root, root) is True
        assert bot_codex._is_relative_to(child, root) is True

    def test_rejects_sibling_with_same_prefix(self, tmp_path):
        root = (tmp_path / "repo").resolve()
        sibling = (tmp_path / "repo2" / "secret.txt").resolve()

        assert bot_codex._is_relative_to(sibling, root) is False

    def test_rejects_tmp_prefix_sibling(self):
        tmp_root = Path("/tmp").resolve()
        tmp_prefix_sibling = Path(str(tmp_root) + "foo").resolve()

        assert bot_codex._is_relative_to(tmp_prefix_sibling, tmp_root) is False


class TestProgressExplanations:
    async def test_agent_message_flushes_before_following_command(self):
        bot = object()
        on_event, state = bot_codex._make_event_handler(123, bot)

        with patch("bot_codex._update_progress", new_callable=AsyncMock) as update_progress:
            await on_event(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I will inspect the working directory first."},
                }
            )
            update_progress.assert_not_awaited()

            await on_event(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": "pwd", "status": "in_progress"},
                }
            )

        assert state["final_text"] == "I will inspect the working directory first."
        update_progress.assert_has_awaits(
            [
                call(123, "I will inspect the working directory first.", bot),
                call(123, "$ pwd", bot),
            ]
        )

    async def test_final_agent_message_is_not_progress_without_more_work(self):
        on_event, state = bot_codex._make_event_handler(123, object())

        with patch("bot_codex._update_progress", new_callable=AsyncMock) as update_progress:
            await on_event({"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}})
            await on_event({"type": "turn.completed", "usage": {}})

        assert state["final_text"] == "Done."
        update_progress.assert_not_awaited()
