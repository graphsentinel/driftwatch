"""Fingerprint a tool call as (tool, scope, argSchemaHash) + category/risk.

Maps to the CFP: `gen_ai.tool.name` + `gen_ai.agent.tool.parameters_hash` +
`gen_ai.agent.tool.category`. The fingerprint is what the detector scores on — never
the resulting API object.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..sdk.observation import ToolCall


@dataclass(frozen=True)
class Fingerprint:
    tool: str
    scope: str
    arg_schema_hash: str
    category: str
    risk: int

    def key(self) -> tuple[str, str, str]:
        return (self.tool, self.scope, self.arg_schema_hash)


def fingerprint(call: ToolCall) -> Fingerprint:
    return Fingerprint(
        tool=call.tool,
        scope=call.scope,
        arg_schema_hash=call.arg_schema_hash(),
        category=call.category,
        risk=call.risk,
    )
