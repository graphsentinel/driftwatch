"""Consensus baseline seeding (FR-9).

A panel of N models each proposes the chain of tool calls it would make for a task; the
*producer* here distills those votes into a baseline seed. The detection logic lives in
`aggregate.py` (pure, cluster-free, LLM-free) — the only new scoring code. Live model
polling (the provider clients) is the roadmap `runner.py` seam; the offline path consumes
already-collected proposals so the quorum logic is fully unit-testable without a network.
"""
from __future__ import annotations

from .aggregate import ConsensusResult, build_consensus, quorum_for

__all__ = ["ConsensusResult", "build_consensus", "quorum_for"]
