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


def test_baseline_poisoning_guard():  # TC-F-25 (NFR-8)
    rec = Reconciler(validate(GOOD_SPEC), persistent=False)
    _fold(rec)  # warm a ready baseline from trusted successfulRuns

    # 1) a drift-suspect chain must NOT auto-fold, even from a trusted source
    drift = DecisionChain(task_type="investigate_latency")
    drift.add(ToolCall(tool="DeleteNamespace", scope="ns/a", category="k8s", risk=4))
    assert rec.observe(drift, source="successfulRuns") is False
    b = rec.store.get("investigate_latency")
    assert "DeleteNamespace" not in b.expected_tools   # baseline not poisoned

    # 2) an untrusted source never auto-folds, even a benign chain
    benign = DecisionChain(task_type="investigate_latency")
    benign.add(ToolCall(tool="QueryMetrics", scope="ns/a", category="observability"))
    assert rec.observe(benign, source="liveTraffic") is False

    # 3) a within-baseline chain from a trusted source DOES fold
    assert rec.observe(benign, source="successfulRuns") is True


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


def test_operator_run_invokes_kopf_run(monkeypatch):
    # Regression: run() called kopf.cli.main(), but `import kopf` does not auto-import
    # the kopf.cli submodule, so the operator CrashLoopBackOff'd at startup with
    # "module 'kopf' has no attribute 'cli'". Guard that run() uses the embedded
    # kopf.run() instead.
    kopf = pytest.importorskip("kopf")
    from driftwatch.operator import main as opmain

    called = {}
    monkeypatch.setattr(kopf, "run", lambda *a, **k: called.update(a=a, k=k))
    opmain.run()
    assert called.get("k", {}).get("standalone") is True


def test_feature_mask_enforced():  # TC-F-23 (FR-2 — detection.features actually applied)
    store = BaselineStore(window=10)
    for _ in range(3):
        c = DecisionChain(task_type="restart_pod")
        c.add(ToolCall(tool="DescribePod", scope="ns/team-a", category="k8s"))
        store.fold(c)
    b = store.get("restart_pod")

    # a known tool but a never-seen scope -> a scope-only deviation
    scope_drift = DecisionChain(task_type="restart_pod")
    scope_drift.add(ToolCall(tool="DescribePod", scope="ns/team-b", category="k8s"))

    # default (all features) catches it as scope_creep
    d_all = score_chain(scope_drift, b, action="block")
    assert d_all.is_drift and d_all.anomaly_kind == "scope_creep"

    # features=[tool] disables the scope feature -> no drift
    d_tool = score_chain(scope_drift, b, action="block", features={"tool"})
    assert not d_tool.is_drift and d_tool.anomaly_kind is None

    # the tool feature still works under the same mask
    tool_drift = DecisionChain(task_type="restart_pod")
    tool_drift.add(ToolCall(tool="DeleteNamespace", scope="ns/team-a", category="k8s"))
    d2 = score_chain(tool_drift, b, action="block", features={"tool"})
    assert d2.is_drift and d2.anomaly_kind == "baseline_mismatch"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_cross_check_parsed_from_policy():
    # E13 §4c — AgentDriftPolicy.crossCheck reconciles into Policy.cross_check (declarative, CRD-driven)
    spec = {**GOOD_SPEC, "crossCheck": {
        "enabled": True, "provider": "anthropic", "model": "claude-3-5-haiku",
        "apiKeySecretRef": {"name": "llm-keys", "key": "anthropic", "envName": "ANTHROPIC_API_KEY"}}}
    p = validate(spec)
    assert p.cross_check["enabled"] is True
    assert p.cross_check["provider"] == "anthropic"
    assert p.cross_check["apiKeySecretRef"]["name"] == "llm-keys"


def test_cross_check_invalid_provider_rejected():
    with pytest.raises(PolicyError, match="crossCheck.provider"):
        validate({**GOOD_SPEC, "crossCheck": {"provider": "cohere"}})
