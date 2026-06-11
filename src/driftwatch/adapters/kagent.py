"""Built-in adapter for Kagent (Solo.io) tool calls.

Kagent emits tool calls as dicts like:
    {"tool": "DeleteNamespace", "namespace": "checkout", "args": {...}}
Normalized into a DriftWatch ToolCall. Built-in => ships with DriftWatch.
"""
from __future__ import annotations

from typing import Any

from ..sdk.observation import RuntimeAdapter, ToolCall


@RuntimeAdapter.register
class KagentAdapter(RuntimeAdapter):
    name = "kagent"

    def normalize(self, raw: Any) -> ToolCall:
        tool = raw.get("tool") or raw.get("name", "")
        scope = raw.get("namespace") or raw.get("scope", "")
        args = raw.get("args") or raw.get("arguments", {}) or {}
        return ToolCall(tool=tool, scope=scope, arguments=dict(args))
