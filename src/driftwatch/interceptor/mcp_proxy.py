"""E7 — chain-aware MCP-proxy enforcement (path B, real Kagent).

DriftWatch sits between an agent runtime and the real MCP tool servers as an MCP proxy, and
scores each caller's *decision chain* — not just the isolated call — before forwarding. The
transport is **not** hand-rolled: FastMCP owns JSON-RPC / `tools/list` / Streamable HTTP /
sessions; we add only one scoring middleware around the existing `Interceptor`.

Verified against fastmcp 3.3.1 (see Docs/e7-mcp-proxy-design.md):
- proxy: `fastmcp.server.create_proxy(backend) -> FastMCP` (the non-deprecated form in
  3.3.1; `FastMCP.as_proxy` exists but is deprecated in favor of it)
- middleware: `fastmcp.server.middleware.Middleware.on_call_tool(self, context, call_next)`
- call fields: `context.message.name` / `context.message.arguments`
- session: `context.fastmcp_context.session_id`
- block: `fastmcp.exceptions.ToolError` (the only deny mechanism we use at the MCP hop)
- drop at the MCP hop also surfaces as a `ToolError` (different message), NOT a synthetic
  success: a fake "success" result would have to satisfy the upstream tool's output schema
  (which the proxy doesn't know), so an error is the schema-agnostic, unambiguous deny. Drop
  stays a silent 200/empty only on the sidecar/HTTP hop. (Docs/e7-mcp-proxy-design.md T-E7.4)

`fastmcp` is an opt-in dependency (`pip install -e '.[mcp]'`), so the imports live inside the
functions — importing this module never forces fastmcp on the core install.
"""
from __future__ import annotations

import os
from typing import Callable

from ..adapters import KagentAdapter
from .engine import BLOCK, DROP, Interceptor
from .mcp_mapping import to_engine_call

# A factory makes a FRESH Interceptor (its own adapter/chain) sharing the process-wide
# baseline store + emitter. One per caller session — see DriftMiddleware.
InterceptorFactory = Callable[[], Interceptor]


def make_session_interceptor_factory(template: Interceptor, task_type: str = "") -> InterceptorFactory:
    """Factory that clones `template`'s shared parts but a fresh per-session chain.

    Shared (read-mostly): baseline store, policy knobs, emitter. Per-session: the adapter,
    which holds that caller's `DecisionChain`. This is the "shared baseline + per-session
    chain" design — we do NOT clone the engine or the store per session.
    """
    def _make() -> Interceptor:
        return Interceptor(
            template.store,                       # shared baseline store
            KagentAdapter(task_type=task_type),   # per-session: fresh chain
            threshold=template.threshold,
            action=template.action,
            failure_policy=template.failure_policy,
            features=template.features,
            emitter=template.emitter,             # shared emitter
        )
    return _make


def _is_session_fault(exc: Exception) -> bool:
    """True for the upstream session-lifecycle class we retry (E10 reconnect, D1).

    Real upstreams (e.g. kubernetes-mcp-server) terminate idle Streamable-HTTP sessions and 404
    on `DELETE /mcp`, so a delayed call reuses a server-closed session. The fault surfaces two
    ways — as a raised `McpError("Session terminated")` OR as a `call_tool_mcp` result with
    `isError=True` whose text says the session is gone — so this is matched on the message text in
    both paths. We retry ONLY this transport class — never a DriftWatch verdict or a genuine
    upstream tool error.
    """
    s = str(exc).lower()
    return "session terminated" in s or "session not found" in s or "session is closed" in s


def _result_error_text(res) -> str:
    """Concatenate the text of an MCP CallToolResult's content blocks (for isError inspection)."""
    parts = []
    for b in getattr(res, "content", None) or []:
        t = getattr(b, "text", None)
        if t:
            parts.append(t)
    return " ".join(parts)


# Conservative write/destructive verbs in a tool name. Used as a SAFETY FALLBACK for the
# reconnect retry guard (D3): at the MCP hop the upstream's tool risk is often unknown
# (the adapter has no catalog → risk=0), so we must NOT infer "safe to retry" from risk alone.
# A tool whose name contains any of these is treated as destructive and is not retried on a
# session fault by default — a transport retry must never double-execute a delete/create/etc.
_DESTRUCTIVE_VERBS = (
    "delete", "remove", "destroy", "drain", "evict", "cordon", "uncordon",
    "create", "apply", "patch", "update", "replace", "edit", "set", "write",
    "put", "post", "scale", "restart", "rollout", "exec", "attach", "cp",
)


def _looks_destructive(tool_name: str, risk: int, destructive_risk: int) -> bool:
    """True if a call should be treated as destructive for the retry guard (D3).

    Destructive if the detector's risk tier says so, OR — as a safety fallback when risk is
    unknown (0, the common case at the MCP hop with no catalog) — if the tool name contains a
    write/destructive verb. Read-shaped tools (list/get/watch/describe/...) are not flagged, so
    they remain retry-eligible.
    """
    if risk >= destructive_risk:
        return True
    low = tool_name.lower()
    return any(v in low for v in _DESTRUCTIVE_VERBS)


def _drift_middleware_class():
    """Build the DriftMiddleware class against the (opt-in) fastmcp API."""
    import asyncio

    from fastmcp.exceptions import ToolError
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools.base import ToolResult

    class DriftMiddleware(Middleware):
        """Scores each `tools/call` against the caller's decision chain, then forwards,
        drops, or blocks — the chain-aware enforcement the per-call gateway layer can't do.

        Multi-upstream (E10): when `upstreams` ({name: target}) is set, the forward is routed
        by us with a **fresh client per call** + session-class retry, instead of the lifespan
        mount's reused session — so a session-dropping upstream is survived (reconnect). Scoring
        still runs exactly once, before the forward; retry re-runs only the upstream call.
        """

        def __init__(self, factory: InterceptorFactory, upstreams: "dict | None" = None,
                     max_retries: int = 2, retry_destructive: bool = False,
                     destructive_risk: int = 3, client_cls=None):
            self._factory = factory
            self._sessions: dict[str, Interceptor] = {}
            self._upstreams = upstreams           # multi-upstream call routing (None = single)
            self._max_retries = max_retries
            self._retry_destructive = retry_destructive
            self._destructive_risk = destructive_risk
            self._client_cls = client_cls         # injectable for tests; default fastmcp.Client

        def _interceptor_for(self, context) -> Interceptor:
            ctx = getattr(context, "fastmcp_context", None)
            sid = getattr(ctx, "session_id", None) if ctx else None
            # No stable key → documented degraded per-call mode: a fresh interceptor per
            # call (no chain accumulation) rather than silently merging unrelated callers
            # into one chain. Streamable HTTP supplies a session id, so this is the rare path.
            if not sid:
                return self._factory()
            itc = self._sessions.get(sid)
            if itc is None:
                itc = self._factory()
                self._sessions[sid] = itc
            return itc

        async def on_call_tool(self, context, call_next):
            itc = self._interceptor_for(context)
            raw = to_engine_call(context.message.name, context.message.arguments)
            verdict = itc.handle(raw)                 # SCORE ONCE (chain appended, decision made)

            if verdict.outcome in (BLOCK, DROP):
                # At the MCP hop both deny verdicts surface as an MCP error so the upstream
                # is never called. Why drop == error here (vs the sidecar/HTTP hop, where
                # drop is a silent 200/empty): a synthetic "success" result must satisfy the
                # upstream tool's *output schema* (e.g. {"result": ...}), which the proxy
                # generally does not know — returning a fake success risks schema-validation
                # failures or, worse, a fabricated value the agent trusts. An MCP error is
                # schema-agnostic and unambiguous. The two verdicts differ only in message.
                # (See Docs/e7-mcp-proxy-design.md T-E7.4 / drop-at-MCP note.)
                d = verdict.decision
                kind = getattr(d, "anomaly_kind", None)
                reason = getattr(d, "reason", None)
                verb = "blocked" if verdict.outcome == BLOCK else "dropped"
                msg = f"{verb} by DriftWatch: decision drift"
                if kind:
                    msg += f" ({kind})"
                if reason:
                    msg += f" — {reason}"
                raise ToolError(msg)

            # forward (within baseline / log / shadow): the call proceeds upstream.
            if self._upstreams is None:
                return await call_next(context)       # single-upstream — unchanged (E7/E8/E9)

            # multi-upstream (E10): route by <server>_<tool>, fresh client + session-class retry.
            risk = itc.adapter.chain.calls[-1].risk if itc.adapter.chain.calls else 0
            return await self._forward_fresh(
                context.message.name, context.message.arguments or {}, risk
            )

        async def _forward_fresh(self, namespaced: str, args: dict, risk: int):
            """Route a namespaced tool call to its upstream with a fresh client, retrying the
            session-fault class only (E10 reconnect; design: Docs/e10-reconnect-design.md)."""
            client_cls = self._client_cls
            if client_cls is None:
                from fastmcp import Client as client_cls  # noqa: N813 — default, injectable in tests

            server, sep, tool = namespaced.partition("_")  # server name has no "_" (guard FR-16)
            url = (self._upstreams or {}).get(server) if sep else None
            if url is None:
                raise ToolError(f"no upstream for tool {namespaced!r} (server {server!r})")

            # D3 idempotency: don't risk double-executing a destructive call on a retry. Risk is
            # often 0 at the MCP hop (no catalog), so fall back to a tool-name heuristic.
            destructive = _looks_destructive(tool, risk, self._destructive_risk)
            attempts = 1 if (destructive and not self._retry_destructive) else self._max_retries + 1

            last: Exception | None = None
            for i in range(attempts):
                err_text = None
                try:
                    async with client_cls(url) as c:   # fresh session per attempt (reconnect)
                        res = await c.call_tool_mcp(tool, args)
                    if not getattr(res, "isError", False):
                        return ToolResult(
                            content=res.content,
                            structured_content=getattr(res, "structuredContent", None),
                        )
                    err_text = _result_error_text(res)  # session fault surfaces here as isError
                except Exception as e:                  # noqa: BLE001 — transport faults raise here
                    err_text = str(e)
                # An error (isError result OR raised exception). Retry only the session class.
                last = ToolError(err_text or "upstream error")
                if _is_session_fault(Exception(err_text or "")) and i < attempts - 1:
                    await asyncio.sleep(0.4 * (i + 1))
                    continue
                raise ToolError(f"upstream error from {namespaced}: {(err_text or '')[:120]}")
            assert last is not None
            raise last

    return DriftMiddleware


def _validate_server_names(upstream: dict) -> None:
    """Guard that multi-upstream tool namespacing is **provably collision-free** (FR-16).

    Tools surface as ``f"{server}_{tool}"``. If a server name contained ``_``, two different
    (server, tool) pairs could collapse to the same namespaced name — e.g. server ``a_b`` + tool
    ``c`` and server ``a`` + tool ``b_c`` both yield ``a_b_c``. Forbidding ``_`` in server names
    (and requiring them unique + alphanumeric/``-``) makes the segment up to the first ``_`` map
    back to exactly one server, so no two tools can collide. Names must also be non-empty.
    """
    names = list(upstream)
    for n in names:
        if not n or "_" in n or not n.replace("-", "").isalnum():
            raise ValueError(
                f"invalid upstream server name {n!r}: server names must be non-empty, "
                "alphanumeric (with '-'), and contain no '_', so per-server tool namespacing "
                "(<server>_<tool>) stays collision-free (FR-16)"
            )
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate upstream server names are not allowed: {names}")


def build_mcp_proxy(upstream, factory: InterceptorFactory):
    """Wire a FastMCP proxy in front of `upstream`(s), with DriftWatch scoring middleware.

    Two shapes (E10 — cross-server, FR-16/FR-17):

    - **Single upstream** (backward compatible): anything `create_proxy` accepts — an upstream
      URL, a FastMCP server, a Client/transport, or an MCPConfig (`{"mcpServers": {...}}`).
      One real MCP ToolServer behind one governance seat (E7/E8).

    - **Multiple upstreams**: a `{server_name: target}` mapping (NOT an MCPConfig). Each target
      is proxied and **mounted under its name**, so the aggregated `tools/list` surfaces the
      union as `<server>_<tool>` (per-server namespacing → tool **name collisions** between
      servers are resolved by construction — FR-16). One scoring middleware sits on the
      aggregator and sees every call with its namespaced name, so the **cross-server
      transition falls out of the existing n-gram**: a hop from one server to another that the
      baseline never saw is a novel transition, gated like any sequence drift (FR-17). The
      whole multi-server chain is one session-correlated `DecisionChain`.

    `factory` produces a per-session Interceptor. Returns the proxy FastMCP app (serve it, or
    drive it with an in-memory Client in tests).
    """
    from fastmcp.server import create_proxy

    # Multi-upstream: a plain {name: target} mapping. An MCPConfig is also a dict but carries
    # "mcpServers" — that goes to create_proxy as a single (FastMCP-namespaced) target.
    if isinstance(upstream, dict) and "mcpServers" not in upstream:
        from contextlib import AsyncExitStack, asynccontextmanager

        from fastmcp import Client, FastMCP

        _validate_server_names(upstream)  # FR-16: keep namespacing provably collision-free

        @asynccontextmanager
        async def _lifespan(app):
            # E10 in-cluster fix. Open ONE long-lived client per upstream for the server's
            # lifetime and mount a proxy over it — instead of letting the proxy open/close a
            # session per request. Real upstreams that 404 on session teardown (e.g.
            # kubernetes-mcp-server on DELETE /mcp) otherwise flake with "Session terminated"
            # the moment a second request reuses a torn-down session; that broke multi-upstream
            # aggregation in-cluster even though it passed against in-memory fakes. One session
            # per upstream, held open for the server's life, is stable. (Single-upstream keeps
            # the simple create_proxy path below, unchanged — E7/E8/E9.)
            async with AsyncExitStack() as stack:
                for name, target in upstream.items():
                    client = await stack.enter_async_context(Client(target))
                    app.mount(create_proxy(client), namespace=name)
                yield

        aggregator = FastMCP("driftwatch-aggregator", lifespan=_lifespan)
        # Pass the upstream map so the middleware routes calls itself (fresh client per call +
        # session-class retry), surviving session-dropping upstreams (E10 reconnect). The
        # lifespan mount above still serves tools/list (discovery) over its long-lived clients.
        aggregator.add_middleware(_drift_middleware_class()(factory, upstreams=upstream))
        return aggregator

    proxy = create_proxy(upstream)
    proxy.add_middleware(_drift_middleware_class()(factory))
    return proxy


def parse_upstreams_env(multi: str, single: str):
    """Resolve the proxy's upstream(s) from env, back-compatibly (E10 deploy wiring).

    - `DRIFTWATCH_UPSTREAMS` (multi, **takes precedence**): a comma-separated list of
      `name=url` pairs → a `{name: url}` mapping that `build_mcp_proxy` mounts under per-server
      namespaces (cross-server). E.g. `k8s=http://a/mcp,policy=http://b/mcp`.
    - else `DRIFTWATCH_UPSTREAM_MCP` (single, back-compat): one URL string → single-upstream proxy.

    Returns a dict for multi, or the single URL string (possibly empty) otherwise.
    """
    multi = (multi or "").strip()
    if multi:
        out: dict[str, str] = {}
        for pair in multi.split(","):
            pair = pair.strip()
            if not pair:
                continue
            name, sep, url = pair.partition("=")
            if not sep or not name.strip() or not url.strip():
                raise ValueError(
                    f"invalid DRIFTWATCH_UPSTREAMS entry {pair!r}: expected 'name=url' pairs "
                    "comma-separated, e.g. 'k8s=http://a/mcp,policy=http://b/mcp'"
                )
            out[name.strip()] = url.strip()
        return out
    return (single or "").strip()


def run() -> None:  # pragma: no cover - console entry point
    """`driftwatch-mcp` entry point: proxy the upstream MCP ToolServer(s) with enforcement."""
    from .main import build_default_interceptor

    upstream = parse_upstreams_env(
        os.environ.get("DRIFTWATCH_UPSTREAMS", ""),
        os.environ.get("DRIFTWATCH_UPSTREAM_MCP", ""),
    )
    task_type = os.environ.get("DRIFTWATCH_TASK_TYPE", "")
    template = build_default_interceptor()  # policy + baseline from env / shared store (FR-10)
    proxy = build_mcp_proxy(upstream, make_session_interceptor_factory(template, task_type))
    proxy.run(transport="http", host="0.0.0.0", port=8000)
