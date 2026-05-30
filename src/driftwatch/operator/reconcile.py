"""Reconcile logic: turn a Policy into a live BaselineStore + computed status.

Pure, cluster-free, unit-testable (TC-F-02). The Kopf handlers in main.py call this
and patch the CRD status with the returned dict.
"""
from __future__ import annotations

from ..db import MemoryBackend, SqliteBackend
from ..library.baseline import BaselineStore
from .policy import Policy


class Reconciler:
    def __init__(self, policy: Policy, persistent: bool = False):
        self.policy = policy
        self.backend = SqliteBackend() if persistent else MemoryBackend()
        self.store: BaselineStore = self.backend.load(window=policy.window)

    def seed_from_models(self, expected_chains: dict[str, list]) -> None:
        """Cold-start bootstrap: fold model-proposed chains as initial baseline (FR-9).

        `expected_chains` maps task_type -> list[DecisionChain]. Real successful runs
        later take over the rolling window. No-op when no `models:` source is declared.
        """
        if not self.policy.model_seed:
            return
        for chains in expected_chains.values():
            for chain in chains:
                self.store.fold(chain)
        self.backend.save(self.store)

    def observe(self, chain) -> None:
        """Fold one real successful run into the baseline."""
        self.store.fold(chain)
        self.backend.save(self.store)

    def status(self) -> dict:
        """The status subresource the operator writes (never the user)."""
        return {
            "baselineReady": self.store.ready(),
            "observedTaskTypes": len(self.store.task_types()),
        }
