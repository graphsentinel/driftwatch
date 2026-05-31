"""Pure MCP tools/call -> engine-dict mapping (E7 / T-E7.2).

No FastMCP, no network, no transport — just dict in, dict out — so the one fiddly bit of the
MCP hop (pulling a scope out of MCP's `arguments`, where it isn't a first-class field) is
unit-testable in isolation. Both the reference MCP proxy (Option A, in its `on_call_tool`
middleware) and a future agentgateway/ext_authz adapter (Option B) call this, so the wire
shape is mapped to the engine in exactly one place.

The output is the dict `RuntimeAdapter.normalize` already eats (see adapters/kagent.py):
`{"tool": ..., "namespace": ..., "args": {...}}`. The adapter itself tolerates `name`/
`arguments` too, but MCP carries scope *inside* `arguments` (`namespace` or `scope`), so we
lift it here rather than rely on a top-level field that MCP never sends.
"""
from __future__ import annotations

from typing import Any

# Keys inside an MCP tools/call `arguments` that name the scope a call targets, in priority
# order. MCP has no standard scope field, so these are the conventional ones agents pass.
_SCOPE_KEYS = ("namespace", "scope")


def to_engine_call(name: str, arguments: "dict[str, Any] | None") -> "dict[str, Any]":
    """Map an MCP tools/call (tool name + arguments) to the engine's normalized dict.

    Args:
        name: the MCP tool name (`tools/call` params `name`).
        arguments: the MCP tool arguments dict (`tools/call` params `arguments`); may be
            None or a non-dict (defensive — a malformed call still yields a scoreable dict).

    Returns:
        `{"tool": name, "namespace": <scope from args, or "">, "args": <args>}` — the shape
        `RuntimeAdapter.normalize` consumes. `args` keeps the full arguments so the
        argSchemaHash feature still sees the real argument shape.
    """
    args: dict[str, Any] = dict(arguments) if isinstance(arguments, dict) else {}
    scope = ""
    for k in _SCOPE_KEYS:
        v = args.get(k)
        if v:
            scope = str(v)
            break
    return {"tool": name or "", "namespace": scope, "args": args}
