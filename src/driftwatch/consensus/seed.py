"""Offline consensus-seed driver (T-C3 / T-C4).

Consumes already-collected model proposals (a JSON file), runs the pure `build_consensus`
quorum aggregation, folds the survivors into a baseline via `Reconciler.seed_from_models`,
persists it to `DRIFTWATCH_DATA_DIR`, and writes a `consensus_seed.json` provenance record.

Live model polling (the provider clients that PRODUCE the proposals JSON) is the roadmap
`runner.py` seam — kept out of here so this path is deterministic and unit-testable with no
network. Proposals JSON shape:

    {"<task>": {"<model>": [["toolA", "toolB"], ...], ...}, ...}

each inner list is one proposed chain (a list of tool names, or {"tool":..,"scope":..}).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..operator.policy import validate
from ..operator.reconcile import Reconciler
from ..sdk.observation import DecisionChain, ToolCall
from .aggregate import build_consensus


def _to_chain(task: str, raw_chain: list) -> DecisionChain:
    c = DecisionChain(task_type=task, runtime="consensus")
    for step in raw_chain:
        if isinstance(step, dict):
            c.add(ToolCall(tool=step["tool"], scope=step.get("scope", ""),
                           arguments=step.get("arguments", {}) or {}))
        else:
            c.add(ToolCall(tool=step))
    return c


def load_proposals(raw: dict) -> "dict[str, dict[str, list[DecisionChain]]]":
    """Turn the proposals JSON into the {task -> {model -> [DecisionChain]}} build input."""
    out: dict[str, dict[str, list[DecisionChain]]] = {}
    for task, per_model in raw.items():
        out[task] = {m: [_to_chain(task, ch) for ch in chains]
                     for m, chains in per_model.items()}
    return out


def seed_from_proposals(proposals_raw: dict, policy_spec: dict, out_dir: str | None = None):
    """Build consensus, fold into a baseline, persist, and write provenance.

    Returns the {task -> ConsensusResult} map. `out_dir` (or DRIFTWATCH_DATA_DIR) receives
    consensus_seed.json; the seeded store is persisted via the Reconciler's backend.
    """
    proposals = load_proposals(proposals_raw)
    results = build_consensus(proposals)
    seed = {task: r.chains for task, r in results.items() if r.seeded}

    policy = validate(policy_spec)
    rec = Reconciler(policy, persistent=bool(os.environ.get("DRIFTWATCH_DATA_DIR")))
    rec.seed_from_models(seed)

    out = out_dir or os.environ.get("DRIFTWATCH_DATA_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        record = {task: r.to_json() for task, r in results.items()}
        (Path(out) / "consensus_seed.json").write_text(json.dumps(record, indent=2))

    return results, rec
