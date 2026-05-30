"""Streaming z-score with a rolling window.

We carry TWO values (CFP Description):
  * raw z-score   — std-devs from the mean; the enforcement gate compares this to `threshold`.
  * normalized    — 1 - e^(-z/k) in [0,1]; the human/dashboard signal emitted as
                    gen_ai.evaluation.score.value.

Deliberately simple statistics over a learned anomaly model: explainable on stage,
cheap in-cluster, debuggable the moment it's wrong.
"""
from __future__ import annotations

import math
from collections import deque


class StreamingZScore:
    """Rolling mean/std over the last `window` numeric observations."""

    def __init__(self, window: int = 50, squash_k: float = 3.0):
        self.window = window
        self.squash_k = squash_k
        self._values: deque[float] = deque(maxlen=window)

    def update(self, value: float) -> None:
        self._values.append(float(value))

    @property
    def ready(self) -> bool:
        return len(self._values) >= 2

    def mean(self) -> float:
        return sum(self._values) / len(self._values) if self._values else 0.0

    def std(self) -> float:
        n = len(self._values)
        if n < 2:
            return 0.0
        m = self.mean()
        var = sum((v - m) ** 2 for v in self._values) / (n - 1)
        return math.sqrt(var)

    def raw_z(self, value: float) -> float:
        s = self.std()
        if s == 0.0:
            return 0.0
        return abs(float(value) - self.mean()) / s

    @staticmethod
    def normalize(raw_z: float, k: float = 3.0) -> float:
        """Monotonic squash of a raw z-score into [0,1] for score.value."""
        return 1.0 - math.exp(-max(0.0, raw_z) / k)

    def score(self, value: float) -> tuple[float, float]:
        z = self.raw_z(value)
        return z, self.normalize(z, self.squash_k)
