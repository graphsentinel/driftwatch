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


def _drift_middleware_class():
    """Build the DriftMiddleware class against the (opt-in) fastmcp API."""
    from fastmcp.exceptions import ToolError
    from fastmcp.server.middleware import Middleware

    class DriftMiddleware(Middleware):
        """Scores each `tools/call` against the caller's decision chain, then forwards,
        drops, or blocks — the chain-aware enforcement the per-call gateway layer can't do."""

        def __init__(self, factory: InterceptorFactory):
            self._factory = factory
            self._sessions: dict[str, Interceptor] = {}

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
            verdict = itc.handle(raw)

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
            # forward (within baseline / log / shadow): the call proceeds upstream
            return await call_next(context)

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
        from fastmcp import FastMCP

        _validate_server_names(upstream)  # FR-16: keep namespacing provably collision-free
        aggregator = FastMCP("driftwatch-aggregator")
        for name, target in upstream.items():
            aggregator.mount(create_proxy(target), namespace=name)
        aggregator.add_middleware(_drift_middleware_class()(factory))
        return aggregator

    proxy = create_proxy(upstream)
    proxy.add_middleware(_drift_middleware_class()(factory))
    return proxy


def run() -> None:  # pragma: no cover - console entry point
    """`driftwatch-mcp` entry point: proxy the upstream MCP ToolServer with enforcement."""
    from .main import build_default_interceptor

    upstream = os.environ.get("DRIFTWATCH_UPSTREAM_MCP", "")
    task_type = os.environ.get("DRIFTWATCH_TASK_TYPE", "")
    template = build_default_interceptor()  # policy + baseline from env / shared store (FR-10)
    proxy = build_mcp_proxy(upstream, make_session_interceptor_factory(template, task_type))
    proxy.run(transport="http", host="0.0.0.0", port=8000)
