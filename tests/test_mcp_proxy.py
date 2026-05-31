# tests/test_mcp_proxy.py
"""E7 — chain-aware MCP-proxy enforcement (path B), driven by an in-memory fake upstream.

No real Kagent / cluster: a FastMCP fake upstream + the in-memory Client exercise the real
proxy + DriftWatch middleware + the real Interceptor. fastmcp is the opt-in `mcp` extra;
under `.[all]` (CI) it is installed so these run (the NFR-7 no-skip guard stays satisfied).

TC-F-16 within-baseline tools/call forwarded + tools/list passthrough;
TC-F-17 drift → MCP error and the upstream is NEVER called; plus drop semantics, per-session
chain isolation, and that scoring happens over the accumulated chain.
"""
import asyncio

import pytest

pytest.importorskip("fastmcp")  # opt-in mcp extra; installed under .[all]

from fastmcp import Client, FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from driftwatch.adapters import KagentAdapter  # noqa: E402
from driftwatch.interceptor.engine import Interceptor  # noqa: E402
from driftwatch.interceptor.mcp_proxy import (  # noqa: E402
    build_mcp_proxy,
    make_session_interceptor_factory,
)
from driftwatch.library.baseline import BaselineStore  # noqa: E402
from driftwatch.sdk.observation import DecisionChain, ToolCall  # noqa: E402


def _ready_store(task="t"):
    # Baseline normal = a chain of repeated QueryLogs calls with the SAME arg shape the calls
    # send ({"namespace": ...}). Two things matter:
    #  - matching arg shape, else a within-baseline call trips arg_schema_novel;
    #  - the QueryLogs->QueryLogs transition must be in-baseline, so two QueryLogs in one
    #    session don't read as a novel transition (the chain-aware path is real, so the
    #    baseline has to actually contain the repeated-call sequence).
    store = BaselineStore(window=50)
    for _ in range(5):
        c = DecisionChain(task_type=task)
        for _ in range(3):
            c.add(ToolCall(tool="QueryLogs", scope="ns/app",
                           arguments={"namespace": "ns/app"}, category="observability"))
        store.fold(c)
    return store


def _fake_upstream():
    up = FastMCP("fake-upstream")
    called = []

    @up.tool
    def QueryLogs(namespace: str = "") -> str:
        called.append("QueryLogs")
        return "LOGS_OK"

    @up.tool
    def DeleteNamespace(namespace: str = "") -> str:
        called.append("DeleteNamespace")
        return "DELETED"

    return up, called


def _proxy(action="block", counting=False):
    template = Interceptor(_ready_store(), KagentAdapter(task_type="t"), action=action)
    factory = make_session_interceptor_factory(template, task_type="t")
    if counting:
        made = {"n": 0}
        inner = factory

        def counted():
            made["n"] += 1
            return inner()

        up, called = _fake_upstream()
        return build_mcp_proxy(up, counted), called, made
    up, called = _fake_upstream()
    return build_mcp_proxy(up, factory), called


def test_tools_list_passthrough():  # TC-F-16 (discovery)
    proxy, _called = _proxy()

    async def go():
        async with Client(proxy) as c:
            return sorted(t.name for t in await c.list_tools())

    assert asyncio.run(go()) == ["DeleteNamespace", "QueryLogs"]


def test_within_baseline_call_forwarded_to_upstream():  # TC-F-16
    proxy, called = _proxy()

    async def go():
        async with Client(proxy) as c:
            r = await c.call_tool("QueryLogs", {"namespace": "ns/app"})
            return getattr(r, "data", None)

    assert asyncio.run(go()) == "LOGS_OK"
    assert called == ["QueryLogs"]  # reached the real upstream


def test_drift_blocked_and_upstream_never_called():  # TC-F-17
    proxy, called = _proxy()

    async def go():
        async with Client(proxy) as c:
            with pytest.raises(ToolError):
                await c.call_tool("DeleteNamespace", {"namespace": "ns/app"})

    asyncio.run(go())
    assert called == []  # the upstream ToolServer NEVER received the drifting call


def test_drop_at_mcp_hop_denies_and_upstream_never_called():
    # At the MCP hop, drop surfaces as an MCP error (like block, different message) — a
    # synthetic "success" can't satisfy the upstream's output schema, so deny is schema-
    # agnostic. The distinguishing signal is the message ("dropped" vs "blocked").
    proxy, called = _proxy(action="drop")

    async def go():
        async with Client(proxy) as c:
            with pytest.raises(ToolError) as ei:
                await c.call_tool("DeleteNamespace", {"namespace": "ns/app"})
            return str(ei.value)

    msg = asyncio.run(go())
    assert "dropped by DriftWatch" in msg          # drop, not block
    assert called == []                            # upstream NOT invoked on drop


def test_within_baseline_still_forwards_under_drop_action():
    # drop action only suppresses drift; a within-baseline call still reaches upstream
    proxy, called = _proxy(action="drop")

    async def go():
        async with Client(proxy) as c:
            r = await c.call_tool("QueryLogs", {"namespace": "ns/app"})
            return getattr(r, "data", None)

    assert asyncio.run(go()) == "LOGS_OK"
    assert called == ["QueryLogs"]


def test_each_session_gets_its_own_chain():
    # chain-aware core: one interceptor (chain) per session, reused within a session.
    proxy, _called, made = _proxy(counting=True)

    async def go():
        async with Client(proxy) as c:            # session 1: two calls -> ONE interceptor
            await c.call_tool("QueryLogs", {"namespace": "ns/app"})
            await c.call_tool("QueryLogs", {"namespace": "ns/app"})
        async with Client(proxy) as c:            # session 2: a fresh interceptor
            await c.call_tool("QueryLogs", {"namespace": "ns/app"})

    asyncio.run(go())
    # 2 distinct sessions -> 2 interceptors (not 3 calls -> 3, not 1 shared)
    assert made["n"] == 2


def test_scoring_is_over_the_accumulated_chain():
    # a within-baseline call, then an out-of-baseline tool in the SAME session: the second
    # is scored as part of the growing chain and blocked; upstream sees only the first.
    proxy, called = _proxy()

    async def go():
        async with Client(proxy) as c:
            await c.call_tool("QueryLogs", {"namespace": "ns/app"})        # forward
            with pytest.raises(ToolError):
                await c.call_tool("DeleteNamespace", {"namespace": "ns/app"})  # block

    asyncio.run(go())
    assert called == ["QueryLogs"]  # only the within-baseline call reached upstream
