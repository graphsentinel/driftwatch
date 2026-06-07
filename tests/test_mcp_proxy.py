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


# --- E10: cross-server chain governance (FR-16 aggregation, FR-17 cross-server transition) ---

def _ready_store_cross(task="x"):
    """Baseline for a cross-server task: the normal order is policy_set_context (on the policy
    server) THEN k8s_namespaces_list (on the kubernetes server). Tool names are the *namespaced*
    names the aggregator surfaces (`<server>_<tool>`), because that is exactly what the
    middleware sees — so the n-gram learns the cross-server transition just like any other."""
    store = BaselineStore(window=50)
    for _ in range(5):
        c = DecisionChain(task_type=task)
        c.add(ToolCall(tool="policy_set_context", arguments={"env": "dev"}, category="policy"))
        c.add(ToolCall(tool="k8s_namespaces_list", arguments={}, category="k8s"))
        store.fold(c)
    return store


def _fake_cross_upstreams():
    k8s = FastMCP("k8s")
    k8s_called = []

    @k8s.tool
    def namespaces_list() -> str:
        k8s_called.append("namespaces_list")
        return "NS_OK"

    @k8s.tool
    def delete_namespace(namespace: str = "") -> str:
        k8s_called.append("delete_namespace")
        return "DELETED"

    policy = FastMCP("policy")
    policy_called = []

    @policy.tool
    def set_context(env: str = "") -> str:
        policy_called.append("set_context")
        return f"ctx={env}"

    return k8s, k8s_called, policy, policy_called


def _cross_proxy(action="block"):
    template = Interceptor(_ready_store_cross(), KagentAdapter(task_type="x"), action=action)
    factory = make_session_interceptor_factory(template, task_type="x")
    k8s, k8s_called, policy, policy_called = _fake_cross_upstreams()
    # multi-upstream: a {name: target} mapping → one aggregated, namespaced surface
    proxy = build_mcp_proxy({"k8s": k8s, "policy": policy}, factory)
    return proxy, k8s_called, policy_called


def test_tc_f_38_cross_server_aggregation_and_within_baseline_forward():  # TC-F-38
    """One proxy fronts TWO MCP servers: tools/list is the union, per-server namespaced; a
    within-baseline cross-server chain is forwarded, each call routed to its real upstream."""
    proxy, k8s_called, policy_called = _cross_proxy()

    async def go():
        async with Client(proxy) as c:
            tools = sorted(t.name for t in await c.list_tools())
            r1 = await c.call_tool("policy_set_context", {"env": "dev"})   # within baseline (1st)
            r2 = await c.call_tool("k8s_namespaces_list", {})              # in-baseline transition
            return tools, getattr(r1, "data", None), getattr(r2, "data", None)

    tools, r1, r2 = asyncio.run(go())
    # FR-16: aggregated, namespaced union — name collisions between servers impossible by design
    assert tools == ["k8s_delete_namespace", "k8s_namespaces_list", "policy_set_context"]
    assert r1 == "ctx=dev" and r2 == "NS_OK"          # both reached their real upstreams
    assert policy_called == ["set_context"] and k8s_called == ["namespaces_list"]


def test_tc_f_39_cross_server_transition_drift_blocked():  # TC-F-39
    """The case no single-server gateway can hold: each tool is within-baseline on its own
    server, but the *hop between servers* is novel. Baseline order is policy→k8s; the reversed
    k8s→policy transition is gated, and the second upstream never sees the call."""
    proxy, k8s_called, policy_called = _cross_proxy()

    async def go():
        async with Client(proxy) as c:
            await c.call_tool("k8s_namespaces_list", {})                   # 1st, within baseline → forward
            with pytest.raises(ToolError):
                await c.call_tool("policy_set_context", {"env": "dev"})    # novel cross-server hop → block

    asyncio.run(go())
    assert k8s_called == ["namespaces_list"]   # the first call reached the k8s upstream
    assert policy_called == []                  # the cross-server drift NEVER reached the policy upstream
    # both tools are individually in-baseline — a per-call gateway on either server would allow each


def test_cross_server_rejects_collision_prone_server_names():  # FR-16 collision guard
    """Namespacing is provably collision-free only if server names carry no '_': server 'a_b' +
    tool 'c' and server 'a' + tool 'b_c' would both yield 'a_b_c'. build_mcp_proxy rejects such
    names up front rather than silently producing an ambiguous tool surface."""
    template = Interceptor(_ready_store_cross(), KagentAdapter(task_type="x"))
    factory = make_session_interceptor_factory(template, task_type="x")
    k8s, _, policy, _ = _fake_cross_upstreams()

    with pytest.raises(ValueError, match="collision-free"):
        build_mcp_proxy({"a_b": k8s, "a": policy}, factory)   # 'a_b' has '_' → rejected

    # a valid pair (no '_', unique) builds fine
    proxy = build_mcp_proxy({"k8s": k8s, "policy": policy}, factory)
    assert proxy is not None


def test_parse_upstreams_env_multi_and_single():  # E10 deploy wiring
    """The driftwatch-mcp entrypoint resolves multi-upstream from DRIFTWATCH_UPSTREAMS
    ('name=url' pairs) and falls back to single DRIFTWATCH_UPSTREAM_MCP (back-compat)."""
    from driftwatch.interceptor.mcp_proxy import parse_upstreams_env

    # multi takes precedence and parses into a {name: url} mapping
    assert parse_upstreams_env("k8s=http://a/mcp,policy=http://b/mcp", "http://ignored") == {
        "k8s": "http://a/mcp",
        "policy": "http://b/mcp",
    }
    # single fallback when multi is empty
    assert parse_upstreams_env("", "http://only/mcp") == "http://only/mcp"
    assert parse_upstreams_env("", "") == ""
    # malformed multi entry is rejected, not silently dropped
    with pytest.raises(ValueError, match="name=url"):
        parse_upstreams_env("k8s=http://a/mcp,broken", "")


def test_is_session_fault_classifies_only_session_class():  # E10 reconnect (D1)
    """Reconnect retries ONLY the upstream session-lifecycle class — never a DriftWatch verdict
    or a genuine upstream tool error."""
    from driftwatch.interceptor.mcp_proxy import _is_session_fault

    assert _is_session_fault(Exception("Session terminated"))
    assert _is_session_fault(Exception("MCP session not found"))
    assert _is_session_fault(Exception("the session is closed"))
    # NOT retried:
    assert not _is_session_fault(Exception("invalid arguments"))
    assert not _is_session_fault(Exception("blocked by DriftWatch: decision drift"))
    assert not _is_session_fault(Exception("upstream error from k8s_pods_delete"))


def test_looks_destructive_falls_back_to_tool_name_when_risk_unknown():  # E10 reconnect (D3)
    """The retry guard must treat write/destructive tools as destructive even when risk is 0
    (the common MCP-hop case with no catalog) — so a transport retry never double-deletes."""
    from driftwatch.interceptor.mcp_proxy import _looks_destructive

    # risk unknown (0), but the name says destructive → guarded
    assert _looks_destructive("pods_delete", 0, 3)
    assert _looks_destructive("create_namespace", 0, 3)
    assert _looks_destructive("scale_deployment", 0, 3)
    assert _looks_destructive("nodes_drain", 0, 3)
    # read-shaped → retry-eligible
    assert not _looks_destructive("namespaces_list", 0, 3)
    assert not _looks_destructive("pods_get", 0, 3)
    assert not _looks_destructive("events_list", 0, 3)
    # risk tier alone is enough even for a read-named tool
    assert _looks_destructive("namespaces_list", 4, 3)


# --- reconnect _forward_fresh retry behavior (D1/D2/D3), driven by a fake faulting client ---

def _fake_result(is_error=False, text="", structured=None):
    """Minimal stand-in for an MCP CallToolResult (isError + text content blocks)."""
    import types as _t
    block = _t.SimpleNamespace(text=text) if text else None
    return _t.SimpleNamespace(
        isError=is_error,
        content=[block] if block else [],
        structuredContent=structured,
    )


def _fake_client_cls(script):
    """A fastmcp.Client-shaped class whose call_tool_mcp follows `script` (one entry per attempt).
    A new instance is created per attempt (fresh client), so attempt state lives in the closure."""
    state = {"n": 0}

    class FakeClient:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def call_tool_mcp(self, tool, args):
            beh = script[min(state["n"], len(script) - 1)]
            state["n"] += 1
            return beh()   # returns a fake result or raises

    return FakeClient, state


def _mw_with(client_cls, **kw):
    from driftwatch.interceptor.mcp_proxy import _drift_middleware_class
    MW = _drift_middleware_class()
    return MW(factory=lambda: None, upstreams={"k8s": "http://k8s/mcp"},
              client_cls=client_cls, **kw)


def test_reconnect_retries_session_fault_then_succeeds():  # D1
    """First attempt hits a session fault (as isError), second succeeds → reconnect retries
    and returns the result."""
    FakeClient, state = _fake_client_cls([
        lambda: _fake_result(is_error=True, text="Session terminated"),
        lambda: _fake_result(text="NS_OK", structured={"ok": True}),
    ])
    mw = _mw_with(FakeClient)
    res = asyncio.run(mw._forward_fresh("k8s_namespaces_list", {}, 0))
    assert state["n"] == 2                         # retried exactly once
    assert getattr(res, "structured_content", None) == {"ok": True}


def test_reconnect_does_not_retry_non_session_error():  # D1
    """A non-session upstream error is surfaced immediately, never retried."""
    FakeClient, state = _fake_client_cls([
        lambda: _fake_result(is_error=True, text="invalid arguments"),
        lambda: _fake_result(text="should-not-reach"),
    ])
    mw = _mw_with(FakeClient)
    with pytest.raises(Exception):  # ToolError
        asyncio.run(mw._forward_fresh("k8s_namespaces_list", {}, 0))
    assert state["n"] == 1                          # no retry


def test_reconnect_does_not_retry_destructive_by_default():  # D3
    """A destructive call (by name) that hits a session fault is NOT retried by default —
    no double-execution."""
    FakeClient, state = _fake_client_cls([
        lambda: _fake_result(is_error=True, text="Session terminated"),
        lambda: _fake_result(text="should-not-reach"),
    ])
    mw = _mw_with(FakeClient)
    with pytest.raises(Exception):  # ToolError
        asyncio.run(mw._forward_fresh("k8s_pods_delete", {}, 0))   # risk 0, name destructive
    assert state["n"] == 1                          # destructive → single attempt, no retry

    # ...but retries when explicitly opted in
    FakeClient2, state2 = _fake_client_cls([
        lambda: _fake_result(is_error=True, text="Session terminated"),
        lambda: _fake_result(text="DELETED"),
    ])
    mw2 = _mw_with(FakeClient2, retry_destructive=True)
    asyncio.run(mw2._forward_fresh("k8s_pods_delete", {}, 0))
    assert state2["n"] == 2                          # opt-in → retried


def test_score_once_across_a_reconnect_retry():  # D2
    """Scoring runs exactly once per tools/call; a session-fault retry re-runs only the upstream
    forward, never the scoring (no phantom chain self-transition)."""
    import types as _t

    from driftwatch.interceptor.engine import FORWARD, Verdict
    from driftwatch.interceptor.mcp_proxy import _drift_middleware_class

    scored = {"n": 0}

    class FakeInterceptor:
        def __init__(self):
            self.adapter = _t.SimpleNamespace(
                chain=_t.SimpleNamespace(calls=[_t.SimpleNamespace(risk=0)]))

        def handle(self, raw):
            scored["n"] += 1
            return Verdict(FORWARD, 200, None, {})

    FakeClient, cstate = _fake_client_cls([
        lambda: _fake_result(is_error=True, text="Session terminated"),
        lambda: _fake_result(text="OK"),
    ])
    MW = _drift_middleware_class()
    mw = MW(factory=FakeInterceptor, upstreams={"k8s": "http://k8s/mcp"}, client_cls=FakeClient)
    ctx = _t.SimpleNamespace(
        message=_t.SimpleNamespace(name="k8s_namespaces_list", arguments={}),
        fastmcp_context=_t.SimpleNamespace(session_id="s1"))

    async def _call_next(c):
        raise AssertionError("multi-upstream must route via _forward_fresh, not call_next")

    asyncio.run(mw.on_call_tool(ctx, _call_next))
    assert scored["n"] == 1        # scored once
    assert cstate["n"] == 2        # forward retried once (reconnect)
