"""Sequence n-gram model over the tool chain.

Catches *order* drift (sequence inversion) and rate-shaped storms — failures that are
invisible per-call. A transition never seen in the baseline scores as drift.
"""
from __future__ import annotations

from collections import defaultdict


class NGramModel:
    """Frequency table of tool transitions, default bigram (n=2)."""

    def __init__(self, n: int = 2):
        self.n = n
        self._counts: dict[tuple[str, ...], int] = defaultdict(int)
        self._total = 0

    def _grams(self, tools: list[str]) -> list[tuple[str, ...]]:
        if len(tools) < self.n:
            return []
        return [tuple(tools[i : i + self.n]) for i in range(len(tools) - self.n + 1)]

    def fit(self, tools: list[str]) -> None:
        for g in self._grams(tools):
            self._counts[g] += 1
            self._total += 1

    def probability(self, gram: tuple[str, ...]) -> float:
        if self._total == 0:
            return 0.0
        return self._counts.get(gram, 0) / self._total

    def novel_transitions(self, tools: list[str]) -> list[tuple[str, ...]]:
        """Grams in this chain that the baseline has never observed."""
        return [g for g in self._grams(tools) if self._counts.get(g, 0) == 0]

    def min_probability(self, tools: list[str]) -> float:
        grams = self._grams(tools)
        if not grams:
            return 1.0
        return min(self.probability(g) for g in grams)
