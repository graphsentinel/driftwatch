"""Reconcile logic: turn a Policy into a live BaselineStore + computed status.

Pure, cluster-free, unit-testable (TC-F-02). The Kopf handlers in main.py call this
and patch the CRD status with the returned dict.
"""
from __future__ import annotations

from ..db import MemoryBackend, SqliteBackend
from ..library.baseline import BaselineStore
from ..library.decision import score_chain
from .policy import Policy

# Sources whose chains are trusted enough to auto-fold into the baseline (NFR-8).
# Real successful runs / approved traces / dry-runs widen "normal"; anything else
# (e.g. live observed traffic that may itself be drifting) must not auto-fold.
TRUSTED_SOURCES = {"approvedTraces", "successfulRuns", "dryRun"}


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

    def observe(self, chain, *, source: str = "successfulRuns") -> bool:
        """Fold one observed chain into the baseline — but only if it's safe to (NFR-8).

        Baseline poisoning guard: an untrusted source, or a chain that the CURRENT
        baseline would score as drift, must NOT redefine "normal". Returns True if the
        chain was folded, False if it was refused.

        - Untrusted source → never auto-fold.
        - Cold start (baseline not ready) → fold to bootstrap (nothing to score against).
        - Ready baseline → fold only if the chain is within baseline (not drift).
        """
        if source not in TRUSTED_SOURCES:
            return False
        baseline = self.store.get(chain.task_type)
        if baseline.ready:
            d = score_chain(chain, baseline, threshold=self.policy.threshold,
                            action=self.policy.action, features=set(self.policy.features))
            if d.is_drift:
                return False  # drift-suspect chain never auto-folds
        self.store.fold(chain)
        self.backend.save(self.store)
        return True

    def status(self) -> dict:
        """The status subresource the operator writes (never the user)."""
        return {
            "baselineReady": self.store.ready(),
            "observedTaskTypes": len(self.store.task_types()),
        }
