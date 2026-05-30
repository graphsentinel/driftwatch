"""The stable, versioned contract a runtime adapter codes against.

A `RuntimeAdapter` turns whatever an agent framework emits (Kagent, Goose, a custom
MCP/OpenAI-tools/HTTP runtime) into a normalized `DecisionChain`. The detection core
never sees the framework — only this shape. Keep this module backward-compatible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    """One normalized tool/MCP call on the agent's decision plane."""

    tool: str                                   # gen_ai.tool.name
    scope: str = ""                             # namespace / resource scope the call targets
    arguments: dict[str, Any] = field(default_factory=dict)
    category: str = ""                          # gen_ai.agent.tool.category (e.g. "k8s")
    risk: int = 0                               # static risk tier 0-5 (0 safe, 4 destructive)
    step_position: int = 0                      # 1-indexed position in the chain

    def arg_schema_hash(self) -> str:
        """Stable hash of the *shape* of the arguments (keys + value types), not values.

        Catches structurally novel inputs the schema allows but the baseline never saw.
        """
        import hashlib

        shape = sorted((k, type(v).__name__) for k, v in self.arguments.items())
        return hashlib.sha256(repr(shape).encode()).hexdigest()[:16]


@dataclass
class DecisionChain:
    """The unit the detector scores: an agent's chain of tool calls for one task."""

    task_type: str                              # gen_ai.agent.task_type
    calls: list[ToolCall] = field(default_factory=list)
    prompt: str = ""                            # the originating prompt (for forensics)
    agent_id: str = ""                          # gen_ai.agent.id
    runtime: str = ""                           # which adapter produced this (kagent/goose/...)
    model: str = ""                             # gen_ai.request.model
    model_capability: float = 0.0               # log2(params B) — for inverse-scaling analysis
    session_id: str = ""

    @property
    def tools(self) -> list[str]:
        return [c.tool for c in self.calls]

    def add(self, call: ToolCall) -> ToolCall:
        call.step_position = len(self.calls)
        self.calls.append(call)
        return call


class RuntimeAdapter:
    """Base class for runtime adapters. Subclass and implement `normalize`.

    Built-ins (`adapters/kagent.py`, `adapters/goose.py`) subclass this; a `custom`
    adapter image points at any MCP/OpenAI-tools/HTTP tool path. No rebuild of the
    detection core is required to add a runtime — that is FR-8.
    """

    _registry: dict[str, type["RuntimeAdapter"]] = {}
    name: str = "base"

    def __init__(self, task_type: str = "", agent_id: str = "", catalog: Optional[dict] = None):
        self.task_type = task_type
        self.agent_id = agent_id
        self.catalog = catalog or {}
        self.chain = DecisionChain(task_type=task_type, agent_id=agent_id, runtime=self.name)

    def normalize(self, raw: Any) -> ToolCall:  # pragma: no cover - abstract
        """Convert one raw framework tool-call event into a normalized ToolCall."""
        raise NotImplementedError

    def observe(self, raw: Any) -> ToolCall:
        """Normalize a raw event, enrich from catalog, and append to the chain."""
        call = self.normalize(raw)
        if not call.category or not call.risk:
            meta = self.catalog.get(call.tool, {})
            call.category = call.category or meta.get("category", "")
            call.risk = call.risk or int(meta.get("risk", 0))
        return self.chain.add(call)

    def reset(self) -> None:
        self.chain = DecisionChain(
            task_type=self.task_type, agent_id=self.agent_id, runtime=self.name
        )

    @classmethod
    def register(cls, adapter_cls: type["RuntimeAdapter"]) -> type["RuntimeAdapter"]:
        cls._registry[adapter_cls.name] = adapter_cls
        return adapter_cls

    @classmethod
    def get(cls, name: str) -> type["RuntimeAdapter"]:
        """Resolve a built-in (`builtin/kagent`) or custom adapter by name."""
        key = name.split("/", 1)[-1] if name.startswith("builtin/") else name
        if key not in cls._registry:
            raise KeyError(f"unknown runtime adapter {name!r}; known: {sorted(cls._registry)}")
        return cls._registry[key]
