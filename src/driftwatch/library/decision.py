"""Score a live DecisionChain against its baseline and decide the action.

For each call: did the agent drift on tool, scope, sequence, or argSchemaHash? The
worst feature sets anomaly.kind; raw z-score vs threshold sets whether it's drift; the
policy action (log/drop/block) is applied. Score on the event; rest on the span (C1).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..sdk.observation import DecisionChain, ToolCall
from .baseline import TaskBaseline
from .fingerprint import fingerprint
from .zscore import StreamingZScore

# anomaly.kind vocabulary — verbatim from Obs-Summit semconv + additive arg_schema_novel
ANOMALY_BASELINE_MISMATCH = "baseline_mismatch"
ANOMALY_SCOPE_CREEP = "scope_creep"
ANOMALY_BLOCKED_TRANSITION = "blocked_transition"
ANOMALY_ARG_SCHEMA_NOVEL = "arg_schema_novel"
ANOMALY_RISK_ESCALATION = "risk_escalation"

# the four detection features a policy may enable via detection.features (FR-2)
ALL_FEATURES = ("tool", "scope", "sequence", "argSchemaHash")


@dataclass
class Decision:
    is_drift: bool
    raw_z: float
    score_value: float            # normalized [0,1] -> gen_ai.evaluation.score.value
    score_label: str              # low|medium|high|critical
    feature: str | None
    anomaly_kind: str | None
    gate_action: str              # log|drop|block (effective action taken)
    gate_blocked: bool
    reason: str
    baseline_match: bool
    expected_tools: list[str] = field(default_factory=list)
    observed_tool: str = ""
    observed_scope: str = ""              # -> gen_ai.agent.tool.scope (via call)
    observed_category: str = ""           # -> gen_ai.agent.tool.category
    observed_arg_hash: str = ""           # -> gen_ai.agent.tool.parameters_hash
    observed_risk: int = 0                # -> gen_ai.agent.tool.risk_severity


def _label(score_value: float) -> str:
    if score_value >= 0.85:
        return "critical"
    if score_value >= 0.6:
        return "high"
    if score_value >= 0.3:
        return "medium"
    return "low"


def score_chain(
    chain: DecisionChain,
    baseline: TaskBaseline,
    *,
    threshold: float = 3.0,
    action: str = "block",
    features: "set[str] | None" = None,
) -> Decision:
    """Score a chain; return the Decision for its single worst (most-drifted) call.

    `features` is the policy's `detection.features` — only those contribute to drift
    (FR-2). None means all four (backward-compatible default).
    """
    active = set(features) if features else set(ALL_FEATURES)
    worst: Decision | None = None
    tools = chain.tools
    novel_transitions = baseline.sequence.novel_transitions(tools)

    for call in chain.calls:
        d = _score_call(call, baseline, novel_transitions, threshold, action, active)
        if worst is None or d.score_value > worst.score_value:
            worst = d

    if worst is None:  # empty chain
        return Decision(
            is_drift=False, raw_z=0.0, score_value=0.0, score_label="low",
            feature=None, anomaly_kind=None, gate_action="log", gate_blocked=False,
            reason="empty chain", baseline_match=True,
        )
    worst.expected_tools = sorted(baseline.expected_tools)
    return worst


def _score_call(
    call: ToolCall,
    baseline: TaskBaseline,
    novel_transitions: list,
    threshold: float,
    action: str,
    features: set[str],
) -> Decision:
    fp = fingerprint(call)
    feature: str | None = None
    kind: str | None = None
    raw_z = 0.0
    reason = "within baseline"

    # feature 1: tool not in the expected set for this task
    if "tool" in features and baseline.expected_tools and fp.tool not in baseline.expected_tools:
        feature, kind = "tool", ANOMALY_BASELINE_MISMATCH
        raw_z = max(raw_z, threshold + 1.0)
        reason = f"tool {fp.tool!r} not in baseline"

    # feature 2: scope never seen for this task
    if (feature is None and "scope" in features
            and fp.scope and baseline.seen_scopes and fp.scope not in baseline.seen_scopes):
        feature, kind = "scope", ANOMALY_SCOPE_CREEP
        raw_z = max(raw_z, threshold + 1.0)
        reason = f"scope {fp.scope!r} never seen for this task"

    # feature 3: sequence transition the baseline never observed.
    # Attribute the drift to the DESTINATION tool (last token of the gram) — the call
    # that *arrived* out of order — not the (innocent) source tool before it.
    if (feature is None and "sequence" in features
            and novel_transitions and any(g[-1] == call.tool for g in novel_transitions)):
        feature, kind = "sequence", ANOMALY_BLOCKED_TRANSITION
        raw_z = max(raw_z, threshold + 1.0)
        reason = f"novel transition into {call.tool!r}"

    # feature 4: structurally novel arguments
    seen = baseline.seen_arg_schemas.get(fp.tool, set())
    if feature is None and "argSchemaHash" in features and seen and fp.arg_schema_hash not in seen:
        feature, kind = "argSchemaHash", ANOMALY_ARG_SCHEMA_NOVEL
        raw_z = max(raw_z, threshold + 0.5)
        reason = f"novel argument schema for {fp.tool!r}"

    # numeric escalation: unusually high risk vs baseline (z-score). Gated by the `tool`
    # feature, since a risk spike is attributed to the tool dimension.
    if "tool" in features and baseline.max_risk.ready:
        z = baseline.max_risk.raw_z(fp.risk)
        if z > raw_z:
            raw_z = z
            if feature is None and z >= threshold:
                feature, kind = "tool", ANOMALY_RISK_ESCALATION
                reason = f"risk {fp.risk} is {z:.1f} sigma above baseline"

    score_value = StreamingZScore.normalize(raw_z)
    is_drift = raw_z >= threshold
    baseline_match = not is_drift

    if not is_drift:
        gate_action, gate_blocked = "log", False
        feature, kind = None, None
    else:
        gate_action = action
        gate_blocked = action in ("drop", "block")

    return Decision(
        is_drift=is_drift,
        raw_z=raw_z,
        score_value=score_value,
        score_label=_label(score_value),
        feature=feature,
        anomaly_kind=kind,
        gate_action=gate_action,
        gate_blocked=gate_blocked,
        reason=reason,
        baseline_match=baseline_match,
        observed_tool=fp.tool,
        observed_scope=fp.scope,
        observed_category=fp.category,
        observed_arg_hash=fp.arg_schema_hash,
        observed_risk=fp.risk,
    )
