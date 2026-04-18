"""Call-site coverage for the #66 nudge-source tag.

Every orchestrator code path that can end up calling
``TmuxComm.nudge`` / ``send_msg`` must pass its own ``source=``
tag. These tests exercise each caller and assert the exact tag
that lands on the tmux_comm mock.

The taxonomy (locked in here):

- ``orch.delivery`` — ``orchestrator/delivery.py`` (reliable nudge
  with ACK + retransmit)
- ``orch.watchdog`` — ``orchestrator/watchdog.py`` (idle alerts,
  manager-requested re-nudges)
- ``orch.router`` — ``orchestrator/router.py`` inbox relay
  fallback when no delivery protocol is configured
- ``orch.lifecycle`` — ``orchestrator/lifecycle.py`` task
  assignment fallback when no delivery protocol is configured
- ``orch.console`` — interactive operator commands (`nudge`,
  `msg`, `broadcast`, `img`)

``orch.taskqueue`` and ``orch.announcer`` are named in the config
comment but do not currently call ``nudge`` directly — they go
through ``delivery`` / ``nats`` respectively. If either starts
calling tmux directly, add a test here.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestDeliverySource:
    @pytest.mark.asyncio
    async def test_monitor_one_shot_nudge_tags_orch_delivery(self):
        from orchestrator.delivery import DeliveryProtocol
        tmux = MagicMock()
        config = {
            "agents": {
                "manager": {"runtime": "claude_code", "role": "monitor"},
                "hub": {"runtime": "claude_code"},
            },
        }
        dp = DeliveryProtocol(tmux_comm=tmux, config=config)
        dp.deliver("manager", reason="test")
        # Monitor one-shot path uses source="orch.delivery".
        tmux.nudge.assert_called_once_with(
            "manager", force=True, source="orch.delivery",
        )


class TestWatchdogSource:
    @pytest.mark.asyncio
    async def test_handle_manager_response_nudge_tags_orch_watchdog(self):
        from orchestrator.watchdog import IdleWatchdog
        tmux = MagicMock()
        wd = IdleWatchdog(
            lifecycle=MagicMock(current_task=None),
            state_machine=MagicMock(current_state="idle", initial_state="idle"),
            nats_client=AsyncMock(),
            tmux_comm=tmux,
            config={"agents": {"hub": {"runtime": "claude_code"}}},
        )
        await wd.handle_manager_response(
            {"agent": "hub", "action": "nudge"},
        )
        tmux.nudge.assert_called_once_with(
            "hub", force=True, source="orch.watchdog",
        )


class TestRouterSource:
    @pytest.mark.asyncio
    async def test_inbox_relay_fallback_nudge_tags_orch_router(self):
        """When no delivery protocol is wired (older orchestrator
        setups / tests), the router falls back to a direct
        tmux_comm.nudge — and that path must still carry a tag."""
        from orchestrator.router import MessageRouter
        tmux = MagicMock()
        router = MessageRouter(
            nats_client=AsyncMock(),
            state_machine=MagicMock(),
            lifecycle_manager=MagicMock(),
            agents={"hub": {"runtime": "claude_code"}},
            tmux_comm=tmux,
            delivery=None,  # force fallback path
        )
        msg = MagicMock()
        msg.subject = "agents.hub.inbox"
        msg.data = json.dumps({
            "type": "agent_message", "from": "dev", "message": "hi",
        }).encode()
        msg.ack = AsyncMock()
        await router._handle_inbox_relay(msg)
        tmux.nudge.assert_called_once_with(
            "hub", force=True, source="orch.router",
        )


class TestLifecycleSource:
    """``orch.lifecycle`` fallback path is covered by the
    existing ``tests/test_task_lifecycle.py::TestAssignToAgent::
    test_nudges_target_agent_via_tmux`` (updated in this PR to
    expect the new ``source="orch.lifecycle"`` kwarg). Left as a
    deliberate no-op here so the taxonomy list stays grepable.
    """


class TestConsoleSource:
    def _make_console(self):
        from orchestrator.console import Console
        tmux = MagicMock()
        # Must come back truthy so send_msg reports success.
        tmux.send_msg.return_value = True
        console = Console(
            config={
                "agents": {
                    "qa": {"runtime": "claude_code"},
                    "dev": {"runtime": "claude_code"},
                },
                "tmux": {"session_name": "demo"},
            },
            state_machine=MagicMock(),
            task_queue=MagicMock(),
            nats_client=MagicMock(),
            tmux_comm=tmux,
            lifecycle_manager=MagicMock(),
        )
        return console, tmux

    def test_nudge_command_tags_orch_console(self):
        console, tmux = self._make_console()
        console.handle_command("nudge qa")
        tmux.nudge.assert_called_once_with("qa", source="orch.console")

    def test_msg_command_tags_orch_console(self):
        console, tmux = self._make_console()
        console.handle_command("msg dev look at this")
        tmux.send_msg.assert_called_once_with(
            "dev", "look at this", source="orch.console",
        )

    def test_broadcast_tags_orch_console_per_agent(self):
        console, tmux = self._make_console()
        console.handle_command("broadcast announce")
        # Broadcast calls send_msg once per agent; each call must
        # carry the console tag.
        for call in tmux.send_msg.call_args_list:
            kwargs = call.kwargs if call.kwargs else {}
            assert kwargs.get("source") == "orch.console", (
                "every broadcast send_msg must carry the console tag; "
                f"offending call: {call}"
            )
