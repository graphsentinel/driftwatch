"""Built-in adapter for Goose tool calls.

Goose emits MCP-style tool calls:
    {"name": "cordon-node", "parameters": {"node": "...", "namespace": "..."}}
Normalized to a DriftWatch ToolCall. Both Kagent and Goose collapse to the same
DecisionChain shape — one policy governs both.
"""
from __future__ import annotations

from typing import Any

from ..sdk.observation import RuntimeAdapter, ToolCall


@RuntimeAdapter.register
class GooseAdapter(RuntimeAdapter):
    name = "goose"

    def normalize(self, raw: Any) -> ToolCall:
        tool = raw.get("name") or raw.get("tool", "")
        params = raw.get("parameters") or raw.get("params", {}) or {}
        scope = params.get("namespace") or params.get("scope") or raw.get("scope", "")
        return ToolCall(tool=tool, scope=scope, arguments=dict(params))
