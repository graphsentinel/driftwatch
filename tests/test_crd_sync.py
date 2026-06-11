"""Guard: each chart CRD copy must stay byte-identical to its canonical one.

Helm can't read files outside the chart dir and rejects symlinks pointing outside it,
so the chart keeps real copies of the CRDs. This test fails if any pair drifts, so the
duplication is safe (no silent skew between `kubectl apply -f deploy/crd/...` and
`helm install`).
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Every CRD shipped by the chart: (canonical, chart-copy). Add a row per new CRD.
CRDS = [
    "agentdriftpolicy.yaml",
    "agenticarchitecture.yaml",   # E11 — configure/declare layer
]


@pytest.mark.parametrize("name", CRDS)
def test_crd_copies_are_identical(name):
    canonical = ROOT / "deploy" / "crd" / name
    chart_copy = ROOT / "deploy" / "helm" / "driftwatch" / name
    assert canonical.exists(), f"missing canonical CRD: {canonical}"
    assert chart_copy.exists(), f"missing chart CRD copy: {chart_copy}"
    assert canonical.read_bytes() == chart_copy.read_bytes(), (
        f"deploy/crd/{name} and the chart copy have drifted. "
        f"Re-sync: cp deploy/crd/{name} deploy/helm/driftwatch/{name}"
    )


@pytest.mark.parametrize("name", CRDS)
def test_chart_copy_is_a_real_file_not_symlink(name):
    # helm package rejects out-of-chart symlinks; ensure we never reintroduce one.
    chart_copy = ROOT / "deploy" / "helm" / "driftwatch" / name
    assert not chart_copy.is_symlink(), f"chart CRD {name} must be a real file, not a symlink"
