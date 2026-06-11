"""Reference for the `custom` adapter path (FR-8).

A user adds a runtime by shipping a class like this and referencing it in
`spec.runtimes[].adapter: custom` — no rebuild of DriftWatch. Here we accept a generic
OpenAI-tools-style call shape.
"""
from __future__ import annotations

from typing import Any

from ..sdk.observation import RuntimeAdapter, ToolCall


@RuntimeAdapter.register
class CustomExampleAdapter(RuntimeAdapter):
    name = "custom-example"

    def normalize(self, raw: Any) -> ToolCall:
        # OpenAI tool-call shape: {"function": {"name": ..., "arguments": {...}}}
        fn = raw.get("function", raw)
        args = fn.get("arguments", {}) or {}
        return ToolCall(
            tool=fn.get("name", ""),
            scope=str(args.get("namespace", args.get("scope", ""))),
            arguments=dict(args),
        )
