"""In-memory backend — the default for kind/k3d and CI. No persistence across restarts."""
from __future__ import annotations

from ..library.baseline import BaselineStore


class MemoryBackend:
    def __init__(self) -> None:
        self._store: BaselineStore | None = None

    def load(self, window: int) -> BaselineStore:
        if self._store is None:
            self._store = BaselineStore(window=window)
        return self._store

    def save(self, store: BaselineStore) -> None:
        self._store = store
