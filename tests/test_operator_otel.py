"""S2 tests — policy validation (TC-F-01), reconcile/status (TC-F-02), OTel schema (TC-F-08)."""
import pytest

from driftwatch.library import BaselineStore, score_chain
from driftwatch.operator import PolicyError, Reconciler, validate
from driftwatch.otel import build_evaluation_event, build_span_attributes
from driftwatch.sdk import DecisionChain, ToolCall

GOOD_SPEC = {
    "_name": "kagent-cluster-ops",
    "selector": {"matchLabels": {"app": "kagent"}},
    "baseline": {"sources": ["approvedTraces", "successfulRuns"], "window": 10},
    "detection": {"features": ["tool", "scope"], "algorithm": "streaming-zscore", "threshold": 3.0},
    "action": "block",
    "failurePolicy": "failClosed",
    "observability": {"otel": {"endpoint": "host.k3d.internal:4317",
                               "emitEvaluationOn": ["baseline_deviation"]}},
}


def test_valid_policy_parses():  # TC-F-01 (positive)
    p = validate(GOOD_SPEC)
    assert p.action == "block" and p.window == 10 and p.threshold == 3.0


@pytest.mark.parametrize("mutate,msg", [
    ({"action": "bogus"}, "action"),
    ({"detection": {"features": ["bad"], "threshold": 3.0}}, "features"),
    ({"failurePolicy": "maybe"}, "failurePolicy"),
    ({"baseline": {"sources": []}}, "sources"),
])
def test_invalid_policy_rejected(mutate, msg):  # TC-F-01 (negative)
    with pytest.raises(PolicyError) as e:
        validate({**GOOD_SPEC, **mutate})
    assert msg in str(e.value)


def test_model_seed_detected():  # FR-9
    spec = {**GOOD_SPEC, "baseline": {"sources": ["successfulRuns", {"models": ["qwen", "gemma"]}]}}
    assert validate(spec).model_seed == ["qwen", "gemma"]


def _fold(rec: Reconciler, n=3):
    for _ in range(n):
        c = DecisionChain(task_type="investigate_latency")
        c.add(ToolCall(tool="QueryMetrics", scope="ns/a", category="observability"))
        c.add(ToolCall(tool="QueryLogs", scope="ns/a", category="observability"))
        rec.observe(c)


def test_reconcile_writes_status():  # TC-F-02
    rec = Reconciler(validate(GOOD_SPEC), persistent=False)
    assert rec.status() == {"baselineReady": False, "observedTaskTypes": 0}
    _fold(rec)
    assert rec.status() == {"baselineReady": True, "observedTaskTypes": 1}


def test_otel_schema_conformance():  # TC-F-08
    store = BaselineStore(window=10)
    for _ in range(3):
        c = DecisionChain(task_type="investigate_latency", agent_id="kagent/ns/sa")
        c.add(ToolCall(tool="QueryMetrics", scope="ns/a", category="observability"))
        c.add(ToolCall(tool="QueryLogs", scope="ns/a", category="observability"))
        store.fold(c)
    b = store.get("investigate_latency")
    attack = DecisionChain(task_type="investigate_latency", agent_id="kagent/ns/sa")
    attack.add(ToolCall(tool="DeleteNamespace", scope="ns/a", category="k8s", risk=4))
    d = score_chain(attack, b, action="block")

    span = build_span_attributes(attack, d, baseline_id="sre.investigate_latency.v1")
    event = build_evaluation_event(d)

    assert span["gen_ai.agent.baseline.id"] == "sre.investigate_latency.v1"
    assert span["gen_ai.agent.computed.anomaly.kind"] == "baseline_mismatch"
    assert span["gen_ai.agent.gate.action"] == "block"
    assert span["gen_ai.agent.gate.blocked"] is True
    assert all("drift." not in k for k in span)            # C1: no drift.* namespace
    assert event["gen_ai.evaluation.name"] == "baseline_deviation"
    assert 0.0 <= event["gen_ai.evaluation.score.value"] <= 1.0
    assert event["gen_ai.evaluation.score.label"] in {"low", "medium", "high", "critical"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
