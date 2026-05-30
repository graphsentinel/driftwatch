"""Guard: the chart's CRD copy must stay byte-identical to the canonical one.

Helm can't read files outside the chart dir and rejects symlinks pointing outside it,
so the chart keeps a real copy of the CRD. This test fails if the two ever drift, so
the duplication is safe (no silent skew between `kubectl apply -f deploy/crd/...` and
`helm install`).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "deploy" / "crd" / "agentdriftpolicy.yaml"
CHART_COPY = ROOT / "deploy" / "helm" / "driftwatch" / "agentdriftpolicy.yaml"


def test_crd_copies_are_identical():
    assert CANONICAL.exists(), f"missing canonical CRD: {CANONICAL}"
    assert CHART_COPY.exists(), f"missing chart CRD copy: {CHART_COPY}"
    assert CANONICAL.read_bytes() == CHART_COPY.read_bytes(), (
        "deploy/crd/agentdriftpolicy.yaml and the chart copy have drifted. "
        "Re-sync: cp deploy/crd/agentdriftpolicy.yaml "
        "deploy/helm/driftwatch/agentdriftpolicy.yaml"
    )


def test_chart_copy_is_a_real_file_not_symlink():
    # helm package rejects out-of-chart symlinks; ensure we never reintroduce one.
    assert not CHART_COPY.is_symlink(), "chart CRD must be a real file, not a symlink"
