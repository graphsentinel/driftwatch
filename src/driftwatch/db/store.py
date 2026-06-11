"""BaselineStore persistence interface + swappable backends.

`db/` holds the *operational* baseline state the detector reads on the hot path —
distinct from `graph/` (forensic Neo4j store). Swap memory -> sqlite -> Postgres
without touching detection.
"""
from __future__ import annotations

from typing import Protocol

from ..library.baseline import BaselineStore


class BaselineBackend(Protocol):
    """Persistence contract. Backends snapshot/restore the in-memory BaselineStore."""

    def load(self, window: int) -> BaselineStore: ...
    def save(self, store: BaselineStore) -> None: ...
