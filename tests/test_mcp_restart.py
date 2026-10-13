"""The MCP connection must be restarted on the same task that opened it."""

import asyncio

import pytest

from chat.mcp_bridge import McpBridge


def _bridge_with_fakes(open_impl, close_impl) -> McpBridge:
    bridge = McpBridge()
    bridge._open = open_impl
    bridge._close = close_impl
    return bridge


def test_restart_uses_the_same_task_that_opened_the_connection():
    tasks = []
    bridge_box = {}

    async def fake_open():
        tasks.append(("open", asyncio.current_task()))
        bridge_box["bridge"]._session = object()
        bridge_box["bridge"].tools = [{"function": {"name": "get_order_status", "description": "order"}}]
        bridge_box["bridge"].error = None

    async def fake_close():
        tasks.append(("close", asyncio.current_task()))
        bridge_box["bridge"]._session = None

    bridge = _bridge_with_fakes(fake_open, fake_close)
    bridge_box["bridge"] = bridge
    try:
        bridge.start()
        bridge.restart()
        assert bridge.ready
        assert [item[0] for item in tasks] == ["open", "close", "open"]
        assert tasks[0][1] is tasks[1][1] is tasks[2][1]
    finally:
        bridge.close()


def test_failed_restart_keeps_the_reason_and_retry_succeeds():
    attempts = {"n": 0}
    bridge_box = {}

    async def fake_open():
        attempts["n"] += 1
        # start() is the first open. The first restart's open fails. The retry works.
        if attempts["n"] == 2:
            raise RuntimeError("port in use")
        bridge_box["bridge"]._session = object()
        bridge_box["bridge"].tools = [{"function": {"name": "search_instructions", "description": "docs"}}]
        bridge_box["bridge"].error = None

    async def fake_close():
        bridge_box["bridge"]._session = None

    bridge = _bridge_with_fakes(fake_open, fake_close)
    bridge_box["bridge"] = bridge
    try:
        bridge.start()
        assert bridge.ready
        with pytest.raises(RuntimeError, match="port in use"):
            bridge.restart()
        assert bridge.ready is False
        assert "port in use" in bridge.error
        bridge.restart()
        assert bridge.ready
        assert bridge.tools[0]["function"]["name"] == "search_instructions"
    finally:
        bridge.close()
