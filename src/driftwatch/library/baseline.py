"""Per-task-type behavioral baseline, learned dynamically from declared sources.

Adapted from the Observability Summit `observation/baseline.py`, generalized to the
DriftWatch four-feature model. A baseline is built from real runs (approved traces,
successful runs, dry-runs) over a rolling window — not hand-authored.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..sdk.observation import DecisionChain
from .fingerprint import fingerprint
from .ngram import NGramModel
from .zscore import StreamingZScore


@dataclass
class TaskBaseline:
    """What 'normal' means for one task type."""

    task_type: str
    window: int = 50
    expected_tools: set[str] = field(default_factory=set)
    seen_scopes: set[str] = field(default_factory=set)
    seen_arg_schemas: dict[str, set[str]] = field(default_factory=dict)
    sequence: NGramModel = field(default_factory=NGramModel)
    chain_length: StreamingZScore = field(default_factory=StreamingZScore)
    max_risk: StreamingZScore = field(default_factory=StreamingZScore)
    runs: int = 0

    def __post_init__(self) -> None:
        # rebind numeric trackers to honor the configured window
        self.chain_length = StreamingZScore(self.window)
        self.max_risk = StreamingZScore(self.window)

    @property
    def ready(self) -> bool:
        """True once enough runs are folded in to score meaningfully (cold-start guard)."""
        return self.runs >= 2

    def fold(self, chain: DecisionChain) -> None:
        """Fold one observed (normal) chain into the rolling baseline."""
        fps = [fingerprint(c) for c in chain.calls]
        for fp in fps:
            self.expected_tools.add(fp.tool)
            if fp.scope:
                self.seen_scopes.add(fp.scope)
            self.seen_arg_schemas.setdefault(fp.tool, set()).add(fp.arg_schema_hash)
        self.sequence.fit([fp.tool for fp in fps])
        self.chain_length.update(len(fps))
        self.max_risk.update(max((fp.risk for fp in fps), default=0))
        self.runs += 1


class BaselineStore:
    """In-process registry of per-task baselines. Persistence is delegated to `db/`."""

    def __init__(self, window: int = 50):
        self.window = window
        self._baselines: dict[str, TaskBaseline] = {}

    def get(self, task_type: str) -> TaskBaseline:
        if task_type not in self._baselines:
            self._baselines[task_type] = TaskBaseline(task_type=task_type, window=self.window)
        return self._baselines[task_type]

    def fold(self, chain: DecisionChain) -> None:
        self.get(chain.task_type).fold(chain)

    def task_types(self) -> list[str]:
        return sorted(self._baselines)

    def ready(self) -> bool:
        """True once every known task type has a ready baseline (drives status.baselineReady)."""
        return bool(self._baselines) and all(b.ready for b in self._baselines.values())
