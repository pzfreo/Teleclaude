"""Tests for codex_code.py — mocked subprocess."""

import asyncio
import json
import os
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import codex_code
from codex_code import (
    CodexAppServerManager,
    CodexCodeManager,
    CodexTurnAborted,
    format_agent_progress,
    format_item_progress,
    looks_like_auth_error,
)


class TestCodexAppServerManager:
    def test_normalizes_app_server_items_for_existing_renderer(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)

        assert manager._normalize_item(
            {"type": "commandExecution", "status": "inProgress", "command": "pytest", "exitCode": 2}
        ) == {
            "type": "command_execution",
            "status": "in_progress",
            "command": "pytest",
            "exitCode": 2,
            "exit_code": 2,
        }
        assert manager._normalize_item({"type": "agentMessage", "text": "Done"})["type"] == "agent_message"

    async def test_run_turn_uses_persistent_thread_and_streams_notifications(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        manager = CodexAppServerManager(owner)
        proc = MagicMock()
        proc.returncode = None
        conn = codex_code._AppServerConnection(proc=proc)
        received = []

        async def on_event(event):
            received.append(event)

        async def request(_chat_id, active_conn, method, params, timeout=30):
            assert active_conn is conn
            assert method == "turn/start"
            assert params["threadId"] == "thr_123"
            assert params["input"] == [{"type": "text", "text": "Run tests"}]
            assert params["sandboxPolicy"] == {"type": "dangerFullAccess"}
            await manager._notification(
                1001,
                conn,
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thr_123",
                        "item": {"type": "agentMessage", "text": "All green"},
                    },
                },
            )
            await manager._notification(
                1001,
                conn,
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thr_123", "turn": {"id": "turn_1", "status": "completed"}},
                },
            )
            return {"turn": {"id": "turn_1", "status": "inProgress"}}

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", side_effect=request),
        ):
            await manager.run_turn(1001, "owner/repo", "Run tests", on_event)

        assert received == [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "All green"}},
            {"type": "turn.completed", "usage": {}},
        ]
        assert conn.active_turn_id is None

    async def test_interrupt_uses_protocol_request_not_process_signal(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        proc = MagicMock()
        proc.returncode = None
        conn = codex_code._AppServerConnection(
            proc=proc,
            active_thread_id="thr_123",
            active_turn_id="turn_456",
            turn_ready=True,
        )
        manager._connections[1001] = conn

        with patch.object(manager, "_request", new_callable=AsyncMock, return_value={}) as request:
            outcome = await manager.interrupt(1001)

        assert outcome == "cancelled"
        request.assert_awaited_once_with(
            1001,
            conn,
            "turn/interrupt",
            {"threadId": "thr_123", "turnId": "turn_456"},
        )
        assert conn.turn_ready is False

    async def test_pending_cancel_never_opens_turn_for_steering(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(
            proc=MagicMock(),
            active_thread_id="thr_123",
            turn_done=asyncio.get_running_loop().create_future(),
        )
        manager._pending_interrupts.add(1001)

        with patch.object(manager, "_interrupt_started_turn", new_callable=AsyncMock):
            await manager._notification(
                1001,
                conn,
                {
                    "method": "turn/started",
                    "params": {"threadId": "thr_123", "turn": {"id": "turn_456"}},
                },
            )

        assert conn.turn_ready is False
        assert await manager.steer(1001, "Do not add this") is False

    async def test_interrupt_marks_turn_cancelled_before_app_server_starts(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)

        assert await manager.interrupt(1001, mark_pending=True) == "cancelled"
        with (
            patch.object(manager, "_start", new_callable=AsyncMock) as start,
            pytest.raises(CodexTurnAborted),
        ):
            await manager.run_turn(1001, "owner/repo", "Never run", AsyncMock())

        start.assert_not_awaited()
        assert 1001 not in manager._pending_interrupts

    async def test_new_operation_can_clear_stale_pending_interrupt(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        manager._pending_interrupts.add(1001)

        manager.clear_pending_interrupt(1001)

        assert 1001 not in manager._pending_interrupts

    async def test_event_handler_failure_does_not_break_turn_completion(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        turn_done = asyncio.get_running_loop().create_future()
        conn = codex_code._AppServerConnection(
            proc=MagicMock(),
            active_thread_id="thr_123",
            on_event=AsyncMock(side_effect=RuntimeError("Telegram unavailable")),
            turn_done=turn_done,
        )

        await manager._notification(
            1001,
            conn,
            {
                "method": "turn/completed",
                "params": {"threadId": "thr_123", "turn": {"id": "turn_1", "status": "completed"}},
            },
        )

        await turn_done

    async def test_turn_start_failure_recycles_connection_with_uncertain_state(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", new_callable=AsyncMock, side_effect=TimeoutError),
            patch.object(manager, "stop", new_callable=AsyncMock) as stop,
            pytest.raises(TimeoutError),
        ):
            await manager.run_turn(1001, "owner/repo", "Run tests", AsyncMock())

        stop.assert_awaited_once_with(1001)
        assert conn.turn_done is None

    async def test_steer_adds_input_to_active_turn(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(
            proc=MagicMock(),
            active_thread_id="thr_123",
            active_turn_id="turn_456",
            turn_ready=True,
            turn_done=asyncio.get_running_loop().create_future(),
        )
        manager._connections[1001] = conn

        with patch.object(
            manager,
            "_request",
            new_callable=AsyncMock,
            return_value={"turnId": "turn_456"},
        ) as request:
            assert await manager.steer(1001, "Also check typing") is True

        request.assert_awaited_once_with(
            1001,
            conn,
            "turn/steer",
            {
                "threadId": "thr_123",
                "expectedTurnId": "turn_456",
                "input": [{"type": "text", "text": "Also check typing"}],
            },
        )

    async def test_steer_returns_false_without_active_turn(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)

        assert await manager.steer(1001, "Too early") is False

    async def test_steer_waits_for_turn_started_notification(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(
            proc=MagicMock(),
            active_thread_id="thr_123",
            active_turn_id="turn_456",
            turn_done=asyncio.get_running_loop().create_future(),
        )
        manager._connections[1001] = conn

        with patch.object(manager, "_request", new_callable=AsyncMock) as request:
            assert await manager.steer(1001, "Still too early") is False

        request.assert_not_awaited()

    async def test_thread_load_failure_recycles_connection_with_uncertain_state(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_request", new_callable=AsyncMock, side_effect=TimeoutError),
            patch.object(manager, "stop", new_callable=AsyncMock) as stop,
            pytest.raises(TimeoutError),
        ):
            await manager._load_thread(1001, conn, "owner/repo", tmp_path, None)

        stop.assert_awaited_once_with(1001)

    @pytest.mark.parametrize("existing_thread", [None, "thr_existing"])
    async def test_thread_load_uses_distinct_kebab_case_sandbox_mode(self, tmp_path, existing_thread):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())
        if existing_thread:
            owner._sessions[(1001, "owner/repo")] = existing_thread

        with patch.object(
            manager,
            "_request",
            new_callable=AsyncMock,
            return_value={"thread": {"id": existing_thread or "thr_new"}},
        ) as request:
            await manager._load_thread(1001, conn, "owner/repo", tmp_path, None)

        params = request.await_args.args[3]
        # The thread sandbox enum is intentionally different from the nested
        # turn SandboxPolicy discriminator asserted by the run-turn test.
        assert params["sandbox"] == "danger-full-access"

    async def test_execute_status_maps_to_thread_read(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(
                manager,
                "_request",
                new_callable=AsyncMock,
                return_value={"thread": {"id": "thr_123", "status": "idle", "model": "gpt-test"}},
            ) as request,
        ):
            response = await manager.execute_slash(1001, "owner/repo", "/status")

        request.assert_awaited_once_with(
            1001,
            conn,
            "thread/read",
            {"threadId": "thr_123", "includeTurns": False},
        )
        assert "Thread: thr_123" in response
        assert "Runtime: idle" in response

    async def test_execute_compact_waits_for_thread_compacted_notification(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        async def request(_chat_id, active_conn, method, params, timeout=30):
            assert active_conn is conn
            assert method == "thread/compact/start"
            assert params == {"threadId": "thr_123"}
            assert conn.compact_done is not None
            assert not conn.compact_done.done()
            await manager._notification(
                1001,
                conn,
                {"method": "thread/compacted", "params": {"threadId": "thr_123"}},
            )
            return {}

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", side_effect=request),
        ):
            response = await manager.execute_slash(1001, "owner/repo", "/compact")

        assert response == "Codex context compaction completed."
        assert conn.compact_done is None

    async def test_execute_compact_consumes_cancel_armed_during_preparation(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        async def load_thread(*_args):
            manager._pending_interrupts.add(1001)
            return "thr_123"

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", side_effect=load_thread),
            patch.object(manager, "_request", new_callable=AsyncMock) as request,
            pytest.raises(CodexTurnAborted),
        ):
            await manager.execute_slash(1001, "owner/repo", "/compact")

        request.assert_not_awaited()
        assert 1001 not in manager._pending_interrupts

    async def test_compact_start_failure_recycles_connection_with_uncertain_state(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", new_callable=AsyncMock, side_effect=TimeoutError),
            patch.object(manager, "stop", new_callable=AsyncMock) as stop,
            pytest.raises(TimeoutError),
        ):
            await manager.execute_slash(1001, "owner/repo", "/compact")

        stop.assert_awaited_once_with(1001)
        assert conn.compact_done is None

    async def test_execute_unknown_slash_does_not_start_model_turn(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", new_callable=AsyncMock) as request,
        ):
            response = await manager.execute_slash(1001, "owner/repo", "/diff")

        request.assert_not_awaited()
        assert "Unsupported Codex stream command: /diff" in response

    async def test_execute_goal_get_maps_to_thread_goal_get(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(
                manager,
                "_request",
                new_callable=AsyncMock,
                return_value={
                    "goal": {
                        "objective": "Ship stream mode",
                        "status": "active",
                        "tokensUsed": 42,
                        "tokenBudget": 1000,
                    }
                },
            ) as request,
        ):
            response = await manager.execute_slash(1001, "owner/repo", "/goal")

        request.assert_awaited_once_with(1001, conn, "thread/goal/get", {"threadId": "thr_123"})
        assert "Objective: Ship stream mode" in response
        assert "Token budget: 1000" in response

    async def test_execute_goal_set_and_clear_use_goal_protocol_methods(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", new_callable=AsyncMock) as request,
        ):
            request.return_value = {
                "goal": {"objective": "Ship it", "status": "active", "tokensUsed": 0, "tokenBudget": None}
            }
            assert "Objective: Ship it" in await manager.execute_slash(1001, "owner/repo", "/goal Ship it")
            request.assert_awaited_with(
                1001,
                conn,
                "thread/goal/set",
                {"threadId": "thr_123", "objective": "Ship it"},
            )

            request.return_value = {"cleared": True}
            assert await manager.execute_slash(1001, "owner/repo", "/goal clear") == "Codex goal cleared."
            request.assert_awaited_with(1001, conn, "thread/goal/clear", {"threadId": "thr_123"})

    async def test_execute_goal_resume_reactivates_a_stalled_goal(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", new_callable=AsyncMock) as request,
        ):
            request.side_effect = [
                {"goal": {"objective": "Ship it", "status": "blocked", "tokensUsed": 7, "tokenBudget": None}},
                {"goal": {"objective": "Ship it", "status": "active", "tokensUsed": 7, "tokenBudget": None}},
            ]
            response = await manager.execute_slash(1001, "owner/repo", "/goal resume")

        assert request.await_args_list[0].args[2:] == ("thread/goal/get", {"threadId": "thr_123"})
        assert request.await_args_list[1].args[2:] == (
            "thread/goal/set",
            {"threadId": "thr_123", "status": "active"},
        )
        assert "Objective: Ship it" in response
        assert "Status: active" in response
        assert "Codex will carry on working towards it." in response

    async def test_execute_goal_resume_without_a_goal_does_not_set_one(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(manager, "_request", new_callable=AsyncMock, return_value={"goal": None}) as request,
        ):
            response = await manager.execute_slash(1001, "owner/repo", "/goal resume")

        request.assert_awaited_once_with(1001, conn, "thread/goal/get", {"threadId": "thr_123"})
        assert response.startswith("No Codex goal is set.")

    async def test_execute_goal_get_hints_at_resume_when_stalled(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock())

        with (
            patch.object(manager, "_start", new_callable=AsyncMock, return_value=conn),
            patch.object(manager, "_load_thread", new_callable=AsyncMock, return_value="thr_123"),
            patch.object(
                manager,
                "_request",
                new_callable=AsyncMock,
                return_value={
                    "goal": {"objective": "Ship it", "status": "usageLimited", "tokensUsed": 9, "tokenBudget": None}
                },
            ),
        ):
            response = await manager.execute_slash(1001, "owner/repo", "/goal")

        assert "Resume it with /goal resume." in response

    async def test_server_started_turn_renders_through_the_background_sink(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock(), active_thread_id="thr_123")
        seen: list[tuple[int, dict]] = []

        async def sink(chat_id, event):
            seen.append((chat_id, event))

        manager.on_background_event = sink
        started = {"method": "turn/started", "params": {"threadId": "thr_123", "turn": {"id": "t1"}}}
        item = {
            "method": "item/completed",
            "params": {"threadId": "thr_123", "item": {"type": "agentMessage", "text": "progress"}},
        }
        completed = {
            "method": "turn/completed",
            "params": {"threadId": "thr_123", "turn": {"id": "t1", "status": "completed"}},
        }
        for message in (started, item, completed):
            await manager._notification(1001, conn, message)

        assert [event["type"] for _, event in seen] == ["turn.started", "item.completed", "turn.completed"]
        assert {chat_id for chat_id, _ in seen} == {1001}
        assert conn.background_turn is False
        assert conn.active_turn_id is None

    async def test_background_turn_leaves_a_pending_client_turn_alone(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock(), active_thread_id="thr_123")
        client_events: list[dict] = []

        async def client_sink(event):
            client_events.append(event)

        async def background_sink(chat_id, event):
            raise AssertionError("client turn must not be treated as a goal continuation")

        manager.on_background_event = background_sink
        conn.turn_done = asyncio.get_running_loop().create_future()
        conn.on_event = client_sink

        await manager._notification(
            1001, conn, {"method": "turn/started", "params": {"threadId": "thr_123", "turn": {"id": "t1"}}}
        )
        await manager._notification(
            1001,
            conn,
            {
                "method": "turn/completed",
                "params": {"threadId": "thr_123", "turn": {"id": "t1", "status": "completed"}},
            },
        )

        assert [event["type"] for event in client_events] == ["turn.started", "turn.completed"]
        assert conn.turn_done is None  # resolved and cleared by the client turn path

    async def test_failed_background_turn_reports_the_error(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock(), active_thread_id="thr_123")
        seen: list[dict] = []

        async def sink(chat_id, event):
            seen.append(event)

        manager.on_background_event = sink
        await manager._notification(
            1001, conn, {"method": "turn/started", "params": {"threadId": "thr_123", "turn": {"id": "t1"}}}
        )
        await manager._notification(
            1001,
            conn,
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_123",
                    "turn": {"id": "t1", "status": "failed", "error": {"message": "boom"}},
                },
            },
        )

        assert seen[-1] == {"type": "turn.failed", "error": {"message": "boom"}}

    async def test_goal_status_hook_fires_only_on_a_change(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock(), active_thread_id="thr_123")
        seen: list[str] = []

        async def sink(chat_id, goal):
            seen.append(goal["status"])

        manager.on_goal_status = sink

        async def update(status):
            await manager._notification(
                1001,
                conn,
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": "thr_123", "goal": {"objective": "Ship it", "status": status}},
                },
            )

        await update("active")
        await update("active")
        await update("blocked")
        await manager._notification(1001, conn, {"method": "thread/goal/cleared", "params": {"threadId": "thr_123"}})

        assert seen == ["active", "blocked"]
        assert conn.goal_status is None

    async def test_interrupted_notification_raises_turn_aborted(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        conn = codex_code._AppServerConnection(proc=MagicMock(), active_thread_id="thr_123")
        turn_done = asyncio.get_running_loop().create_future()
        conn.turn_done = turn_done

        await manager._notification(
            1001,
            conn,
            {
                "method": "turn/completed",
                "params": {"threadId": "thr_123", "turn": {"status": "interrupted"}},
            },
        )

        with pytest.raises(CodexTurnAborted):
            await turn_done

    async def test_completed_notification_rejects_nonterminal_status(self, tmp_path):
        owner = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        manager = CodexAppServerManager(owner)
        turn_done = asyncio.get_running_loop().create_future()
        conn = codex_code._AppServerConnection(
            proc=MagicMock(),
            active_thread_id="thr_123",
            turn_done=turn_done,
        )

        await manager._notification(
            1001,
            conn,
            {
                "method": "turn/completed",
                "params": {"threadId": "thr_123", "turn": {"status": "inProgress"}},
            },
        )

        with pytest.raises(RuntimeError, match="Unexpected Codex app-server terminal status"):
            await turn_done


class TestCodexCodeManager:
    def test_workspace_path(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("owner/repo")
        assert path == tmp_path / "owner" / "repo"

    def test_workspace_path_nested(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("my-org/my-project")
        assert path == tmp_path / "my-org" / "my-project"

    def test_available_with_cli(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        assert mgr.available is True

    def test_not_available_without_cli(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path=None)
        mgr.cli_path = None
        assert mgr.available is False

    def test_session_management(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        assert mgr.get_session_id(1001, "owner/repo") is None
        mgr._sessions[(1001, "owner/repo")] = "thread-abc"
        assert mgr.get_session_id(1001, "owner/repo") == "thread-abc"

    def test_new_session_clears(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._sessions[(1001, "owner/repo")] = "thread-abc"
        mgr.new_session(1001, "owner/repo")
        assert mgr.get_session_id(1001, "owner/repo") is None

    def test_new_session_noop_if_no_session(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr.new_session(9999, "owner/repo")  # should not raise
        assert mgr.get_session_id(9999, "owner/repo") is None

    def test_sessions_isolated_per_repo(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._sessions[(1001, "owner/repo-a")] = "thread-a"
        mgr._sessions[(1001, "owner/repo-b")] = "thread-b"
        mgr.new_session(1001, "owner/repo-a")
        assert mgr.get_session_id(1001, "owner/repo-a") is None
        assert mgr.get_session_id(1001, "owner/repo-b") == "thread-b"

    def test_default_workspace_root(self):
        mgr = CodexCodeManager("fake-token")
        assert mgr.workspace_root == Path("workspaces-codex")

    def test_has_running_proc_false_by_default(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        assert mgr.has_running_proc(1001) is False

    async def test_abort_no_proc_returns_false(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        assert await mgr.abort(1001) is False

    async def test_abort_can_mark_pending_turn_without_proc(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))

        assert await mgr.abort(1001, mark_pending=True) is True
        assert 1001 in mgr._aborted_chats

    @staticmethod
    def _exiting_proc(sig):
        class _RunningProc:
            pid = 4321
            returncode = None

            async def wait(self):
                self.returncode = -sig
                return self.returncode

        return _RunningProc()

    async def test_abort_signals_process_group(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._running_procs[1001] = self._exiting_proc(signal.SIGTERM)  # type: ignore[assignment]

        with patch("os.killpg") as killpg:
            assert await mgr.abort(1001) is True

        killpg.assert_called_once_with(4321, signal.SIGTERM)
        assert 1001 not in mgr._running_procs

    async def test_abort_marks_chat_only_while_a_turn_is_in_flight(self, tmp_path):
        """Regression: /stop on a detached process must not poison the *next* message.

        `_aborted_chats` is consumed by run_turn. Setting it when no turn is
        reading events left the flag behind, and the next message the user sent
        was aborted before it reached Codex — the bot just went silent.
        """
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._running_procs[1001] = self._exiting_proc(signal.SIGTERM)  # type: ignore[assignment]

        with patch("os.killpg"):
            assert await mgr.abort(1001) is True
        assert 1001 not in mgr._aborted_chats

        mgr._running_procs[1001] = self._exiting_proc(signal.SIGTERM)  # type: ignore[assignment]
        mgr._abort_events[1001] = asyncio.Event()  # a turn is now reading events
        with patch("os.killpg"):
            assert await mgr.abort(1001) is True
        assert 1001 in mgr._aborted_chats

    async def test_clear_pending_abort_drops_a_stale_flag(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._aborted_chats.add(1001)
        mgr.clear_pending_abort(1001)
        assert 1001 not in mgr._aborted_chats

    async def test_interrupt_sigints_process_group_and_marks_chat_aborted(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._running_procs[1001] = self._exiting_proc(signal.SIGINT)  # type: ignore[assignment]
        mgr._abort_events[1001] = asyncio.Event()

        with patch("os.killpg") as killpg:
            assert await mgr.interrupt(1001) == "cancelled"

        # SIGINT must reach the process group: the `codex` entry point is an npm
        # shim, so signalling the parent alone does not stop the real binary.
        killpg.assert_called_once_with(4321, signal.SIGINT)
        assert 1001 not in mgr._running_procs
        assert 1001 in mgr._aborted_chats

    async def test_interrupt_force_kills_when_sigint_ignored(self, tmp_path):
        """Reporting a cancel that didn't happen is worse than killing the process."""
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))

        class _StubbornProc:
            pid = 4321
            returncode = None

            async def wait(self):
                await asyncio.Event().wait()  # never exits

        mgr._running_procs[1001] = _StubbornProc()  # type: ignore[assignment]

        with (
            patch("codex_code.PROCESS_INTERRUPT_GRACE", 0.01),
            patch("codex_code.PROCESS_ABORT_TIMEOUT", 0.01),
            patch("os.killpg") as killpg,
        ):
            assert await mgr.interrupt(1001) == "forced"

        assert [c.args[1] for c in killpg.call_args_list] == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
        assert 1001 not in mgr._running_procs

    async def test_interrupt_releases_reader_blocked_on_open_pipe(self, tmp_path):
        """Regression: an orphaned child holding stdout must not wedge the turn forever."""
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        abort_event = asyncio.Event()
        mgr._abort_events[1001] = abort_event

        assert await mgr.interrupt(1001, mark_pending=True) == "cancelled"
        assert abort_event.is_set()

    async def test_interrupt_reports_idle_when_nothing_is_running(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        assert await mgr.interrupt(1001) == "idle"
        assert 1001 not in mgr._aborted_chats

    async def test_interrupt_can_mark_pending_turn_without_proc(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))

        assert await mgr.interrupt(1001, mark_pending=True) == "cancelled"
        assert 1001 in mgr._aborted_chats

    async def test_stop_kills_orphans_left_behind_by_a_reaped_shim(self, tmp_path):
        """The npm shim can exit while the real binary keeps running in its group."""
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._pgids[1001] = 4321  # group recorded at spawn; parent already reaped

        alive = {"n": 2}  # survives the probe, then SIGTERM, then reports gone

        def fake_killpg(pgid, sig):
            assert pgid == 4321
            if sig == 0 and alive["n"] <= 0:
                raise ProcessLookupError
            if sig == signal.SIGTERM:
                alive["n"] = 0

        with patch("os.killpg", side_effect=fake_killpg), patch("codex_code._ORPHAN_POLL_INTERVAL", 0.001):
            assert mgr.has_running_proc(1001) is True
            assert await mgr.abort(1001) is True

        assert 1001 not in mgr._pgids
        assert mgr.has_running_proc(1001) is False

    async def test_orphan_cleanup_never_signals_the_bots_own_group(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        mgr._pgids[1001] = os.getpgrp()

        with patch("os.killpg") as killpg:
            assert await mgr._kill_orphan_group(1001) is False

        assert killpg.call_args_list == [((os.getpgrp(), 0), {})]
        assert 1001 not in mgr._pgids


class TestTokenNotInCloneUrl:
    async def test_ensure_clone_url_does_not_contain_token(self, tmp_path):
        token = "ghp_SUPERSECRETTOKEN123"
        mgr = CodexCodeManager(token, workspace_root=str(tmp_path))

        with patch.object(mgr, "_git", new_callable=AsyncMock) as mock_git:
            await mgr.ensure_clone("owner/repo")

        mock_git.assert_called_once()
        args = mock_git.call_args[0]
        assert args[1] == "clone"
        clone_url = args[2]
        assert token not in clone_url
        assert clone_url == "https://github.com/owner/repo.git"

    def test_git_env_uses_credential_helper(self, tmp_path):
        token = "ghp_SECRET"
        mgr = CodexCodeManager(token, workspace_root=str(tmp_path))
        env = mgr._git_env()

        assert env.get("GIT_CONFIG_COUNT") == "1"
        assert env.get("GIT_CONFIG_KEY_0") == "credential.helper"
        assert token in env.get("GIT_CONFIG_VALUE_0", "")

    def test_git_env_empty_token(self, tmp_path):
        mgr = CodexCodeManager("", workspace_root=str(tmp_path))
        env = mgr._git_env()
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env

    async def test_sanitize_remote_rewrites_tainted_url(self, tmp_path):
        token = "ghp_LEAKEDTOKEN"
        mgr = CodexCodeManager(token, workspace_root=str(tmp_path))
        path = tmp_path / "owner" / "repo"
        (path / ".git").mkdir(parents=True)

        calls: list[tuple] = []

        async def fake_git(cwd, *args):
            calls.append((cwd, args))
            if args[:3] == ("remote", "get-url", "origin"):
                return f"https://x-access-token:{token}@github.com/owner/repo.git"
            return ""

        with patch.object(mgr, "_git", side_effect=fake_git):
            await mgr.ensure_clone("owner/repo")

        set_url_calls = [c for c in calls if c[1][:3] == ("remote", "set-url", "origin")]
        assert len(set_url_calls) == 1
        assert set_url_calls[0][1][3] == "https://github.com/owner/repo.git"


class TestPathTraversal:
    def test_dotdot_in_owner(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        with pytest.raises(ValueError, match="Path traversal blocked"):
            mgr.workspace_path("../../etc/passwd")

    def test_valid_repo_allowed(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path))
        path = mgr.workspace_path("legit-org/legit-repo")
        assert str(path).startswith(str(tmp_path))


class TestFormatItemProgress:
    def test_command_execution_in_progress(self):
        line = format_item_progress({"type": "command_execution", "command": "echo hi", "status": "in_progress"})
        assert line == "$ echo hi"

    def test_command_execution_completed_success_is_silent(self):
        line = format_item_progress(
            {"type": "command_execution", "command": "echo hi", "status": "completed", "exit_code": 0}
        )
        assert line is None

    def test_command_execution_completed_failure_reported(self):
        line = format_item_progress(
            {"type": "command_execution", "command": "false", "status": "completed", "exit_code": 1}
        )
        assert line == "$ command exited 1"

    def test_agent_message_is_not_a_progress_line(self):
        assert format_item_progress({"type": "agent_message", "text": "hello"}) is None

    def test_error_item(self):
        line = format_item_progress({"type": "error", "message": "boom"})
        assert line == "⚠️ boom"

    def test_unknown_item_type(self):
        assert format_item_progress({"type": "reasoning", "text": "thinking..."}) is None


class TestFormatAgentProgress:
    def test_first_non_empty_line(self):
        assert (
            format_agent_progress("\nI will inspect the config first.\n\nMore detail.")
            == "I will inspect the config first."
        )

    def test_empty_text_is_silent(self):
        assert format_agent_progress("   \n") is None

    def test_long_text_is_truncated(self):
        text = "a" * 400
        line = format_agent_progress(text)
        assert line is not None
        assert len(line) == 281
        assert line.endswith("…")


class TestLooksLikeAuthError:
    def test_none_is_false(self):
        assert looks_like_auth_error(None) is False

    def test_specific_phrase_matches(self):
        assert looks_like_auth_error("Please run `codex login` to continue") is True

    def test_bare_401_without_context_is_false(self):
        assert looks_like_auth_error("the meeting room is 401") is False

    def test_401_with_api_context_is_true(self):
        assert looks_like_auth_error('{"type":"error","status":401,"error":{"type":"invalid_request_error"}}') is True


class TestRunTurn:
    """run_turn spawns one subprocess per call — mocked at asyncio.create_subprocess_exec."""

    @staticmethod
    def _fake_proc(stdout_lines: list[bytes], returncode: int = 0):
        class _FakeStdin:
            def __init__(self):
                self.data = bytearray()
                self.closed = False

            def write(self, data):
                self.data.extend(data)

            async def drain(self):
                pass

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        class _FakeStream:
            def __init__(self, lines):
                self._lines = list(lines)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._lines:
                    return self._lines.pop(0)
                raise StopAsyncIteration

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

        class _FakeProc:
            def __init__(self):
                self.pid = 4321
                self.stdin = _FakeStdin()
                self.stdout = _FakeStream(stdout_lines)
                self.stderr = _FakeStream([])
                self.returncode = returncode

            async def wait(self):
                return self.returncode

        return _FakeProc()

    async def test_captures_thread_id_and_builds_fresh_command(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        events = [
            {"type": "thread.started", "thread_id": "thread-123"},
            {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "hi"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10}},
        ]
        lines = [(json.dumps(e) + "\n").encode() for e in events]
        proc = self._fake_proc(lines)

        captured: dict[str, list] = {}

        async def fake_create(*args, **_kwargs):
            captured["cmd"] = list(args)
            captured["kwargs"] = _kwargs
            return proc

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/local/bin/codex"
        assert "exec" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "resume" not in cmd
        assert "hello" not in cmd
        assert cmd[-1] == "-"
        assert captured["kwargs"]["start_new_session"] is True
        assert proc.stdin.data == b"hello"
        assert proc.stdin.closed is True
        assert mgr.get_session_id(1001, "owner/repo") == "thread-123"
        assert len(received) == 3

    async def test_pending_abort_prevents_process_launch(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)
        mgr._aborted_chats.add(1001)

        async def on_event(event):
            pass

        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock()) as create_proc,
            pytest.raises(CodexTurnAborted),
        ):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        create_proc.assert_not_awaited()
        assert 1001 not in mgr._aborted_chats

    async def test_abort_during_process_launch_terminates_created_proc(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)
        proc = self._fake_proc([], returncode=None)

        async def fake_create(*_args, **_kwargs):
            mgr._aborted_chats.add(1001)
            return proc

        async def on_event(event):
            pass

        with (
            patch("asyncio.create_subprocess_exec", new=fake_create),
            patch("os.killpg") as killpg,
            pytest.raises(CodexTurnAborted),
        ):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        killpg.assert_called_once_with(4321, signal.SIGTERM)
        assert proc.stdin.data == b""
        assert proc.stdin.closed is True
        assert 1001 not in mgr._running_procs
        assert 1001 not in mgr._aborted_chats

    async def test_interrupt_ends_turn_when_stdout_never_closes(self, tmp_path):
        """Regression for the wedged-chat bug.

        `/cancel` SIGKILLed only the npm shim, orphaning the real Codex binary,
        which kept the inherited stdout pipe open. run_turn blocked on the read
        forever and never released the caller's per-chat lock, so the bot stopped
        responding until it was restarted.
        """
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        proc = self._fake_proc([], returncode=None)

        async def never_returns():
            await asyncio.Event().wait()

        proc.stdout.readline = never_returns  # orphaned child holds the pipe open
        proc.wait = never_returns  # SIGINT ignored, process outlives the turn

        async def fake_create(*_args, **_kwargs):
            return proc

        async def on_event(event):
            pass

        async def run():
            with patch("asyncio.create_subprocess_exec", new=fake_create):
                await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        turn = asyncio.create_task(run())
        await asyncio.sleep(0)  # let the turn reach the read loop

        with patch("codex_code.PROCESS_INTERRUPT_GRACE", 0.01), patch("codex_code.PROCESS_ABORT_TIMEOUT", 0.01):
            with patch("os.killpg"):
                assert await mgr.interrupt(1001) == "forced"

            with pytest.raises(CodexTurnAborted):
                await asyncio.wait_for(turn, timeout=2)

        assert 1001 not in mgr._abort_events

    async def test_new_turn_kills_process_abandoned_by_soft_cancel(self, tmp_path):
        """/cancel can leave a SIGINT-ignoring process alive; it must not outlive the next turn."""
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        class _StaleProc:
            pid = 9999
            returncode = None

            async def wait(self):
                self.returncode = -signal.SIGKILL
                return self.returncode

        mgr._running_procs[1001] = _StaleProc()  # type: ignore[assignment]

        async def fake_create(*_args, **_kwargs):
            return self._fake_proc([])

        async def on_event(event):
            pass

        with patch("asyncio.create_subprocess_exec", new=fake_create), patch("os.killpg") as killpg:
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        # Plus a signal-0 probe of the new turn's own group as it drains.
        assert call(9999, signal.SIGTERM) in killpg.call_args_list

    async def test_clean_turn_forgets_its_process_group(self, tmp_path):
        """Background work a completed turn started on purpose must survive.

        The group id is only retained for turns that ended abnormally, so the
        orphan sweep can't reach, say, a dev server the user asked Codex to run.
        """
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        mgr.workspace_path("owner/repo").mkdir(parents=True)

        async def fake_create(*_args, **_kwargs):
            return self._fake_proc([])

        async def on_event(event):
            pass

        with patch("asyncio.create_subprocess_exec", new=fake_create), patch("os.killpg"):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert 1001 not in mgr._pgids
        assert mgr.has_running_proc(1001) is False

    async def test_nonzero_exit_still_reported_after_clean_eof(self, tmp_path):
        """The bounded wait added for soft cancels must not swallow a real failure."""
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        proc = self._fake_proc([], returncode=None)

        async def wait():
            # Slower than the abort-path budget, well inside the clean-EOF one.
            await asyncio.sleep(0.05)
            proc.returncode = 2
            return 2

        proc.wait = wait

        async def fake_create(*_args, **_kwargs):
            return proc

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        with (
            patch("asyncio.create_subprocess_exec", new=fake_create),
            patch("codex_code.PROCESS_ABORT_TIMEOUT", 0.01),
            patch("codex_code.PROCESS_EXIT_WAIT", 2),
        ):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert received == [{"type": "_process_error", "returncode": 2, "stderr": ""}]

    async def test_resume_uses_stored_session_id(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)
        mgr._sessions[(1001, "owner/repo")] = "thread-existing"

        proc = self._fake_proc([])
        captured: dict[str, list] = {}

        async def fake_create(*args, **_kwargs):
            captured["cmd"] = list(args)
            return proc

        async def on_event(event):
            pass

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "follow up", on_event)

        cmd = captured["cmd"]
        idx = cmd.index("resume")
        assert cmd[idx + 1] == "thread-existing"
        assert "follow up" not in cmd
        assert cmd[idx + 2] == "-"
        assert proc.stdin.data == b"follow up"
        assert proc.stdin.closed is True

    async def test_nonzero_exit_emits_process_error_event(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        proc = self._fake_proc([], returncode=1)

        async def fake_create(*args, **_kwargs):
            return proc

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert any(e.get("type") == "_process_error" for e in received)

    async def test_aborted_turn_suppresses_buffered_events(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        class _AbortStream:
            def __init__(self):
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                line = await self.readline()
                if not line:
                    raise StopAsyncIteration
                return line

            async def readline(self):
                if self.sent:
                    return b""
                self.sent = True
                mgr._aborted_chats.add(1001)
                return b'{"type":"item.completed","item":{"type":"agent_message","text":"late"}}\n'

        class _FakeProc:
            pid = 4321
            returncode = -signal.SIGTERM

            def __init__(self):
                base = TestRunTurn._fake_proc([])
                self.stdin = base.stdin
                self.stderr = base.stderr
                self.stdout = _AbortStream()

            async def wait(self):
                return self.returncode

        async def fake_create(*_args, **_kwargs):
            return _FakeProc()

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        with patch("asyncio.create_subprocess_exec", new=fake_create), pytest.raises(CodexTurnAborted):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert received == []

    async def test_running_proc_cleared_after_turn(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)
        proc = self._fake_proc([])

        async def fake_create(*args, **_kwargs):
            return proc

        async def on_event(event):
            pass

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert mgr.has_running_proc(1001) is False


class TestOversizedLines:
    """A JSONL event bigger than the stream buffer must not kill the reader.

    asyncio's readline() raises ValueError ("Separator is not found, and chunk
    exceed the limit") once a line passes the subprocess buffer limit, which
    used to surface to the user as a failed turn.
    """

    class _OverflowStream:
        """Raises like an over-limit readline() before yielding the real lines."""

        def __init__(self, lines):
            self._lines = list(lines)
            self.overflowed = False

        async def readline(self):
            if not self.overflowed:
                self.overflowed = True
                raise ValueError("Separator is not found, and chunk exceed the limit")
            return self._lines.pop(0) if self._lines else b""

    async def test_read_line_skips_oversized_line(self):
        stream = self._OverflowStream([b"next\n"])
        assert await codex_code._read_line(stream, "stdout") == b"next\n"

    async def test_iter_lines_skips_oversized_line(self):
        stream = self._OverflowStream([b"a\n", b"b\n"])
        assert [line async for line in codex_code._iter_lines(stream, "stdout")] == [b"a\n", b"b\n"]

    async def test_run_turn_survives_oversized_event(self, tmp_path):
        mgr = CodexCodeManager("fake-token", workspace_root=str(tmp_path), cli_path="/usr/local/bin/codex")
        repo_dir = mgr.workspace_path("owner/repo")
        repo_dir.mkdir(parents=True)

        proc = TestRunTurn._fake_proc([])
        proc.stdout = self._OverflowStream([b'{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'])

        async def fake_create(*_args, **_kwargs):
            return proc

        received: list[dict] = []

        async def on_event(event):
            received.append(event)

        with patch("asyncio.create_subprocess_exec", new=fake_create):
            await mgr.run_turn(1001, "owner/repo", "hello", on_event)

        assert [e["item"]["text"] for e in received] == ["hi"]
