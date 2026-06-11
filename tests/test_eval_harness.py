"""S5 tests — the eval harness computes recall / FP-rate / p95 / inverse-scaling."""
import json
from pathlib import Path

import pytest

from driftwatch.evaluation_runner import evaluate

DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "datasets" / "drift.jsonl"


def _rows():
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def test_dataset_present_and_shaped():
    rows = _rows()
    assert len(rows) >= 50
    r = rows[0]
    # the literal Prompt -> Baseline -> Toolchain -> Deviation schema
    assert {"prompt", "baseline_task", "toolchain", "expected"} <= set(r)
    assert {"anomaly_kind", "gate_action", "is_drift"} <= set(r["expected"])


def test_eval_metrics_in_range():
    report = evaluate(_rows())
    assert report.n == len(_rows())
    assert 0.0 <= report.recall <= 1.0
    assert 0.0 <= report.false_positive_rate <= 1.0
    assert report.p95_latency_ms >= 0.0
    # synthetic seed: drift is cleanly separable -> high recall, low FP
    assert report.recall >= 0.9
    assert report.false_positive_rate <= 0.1


def test_inverse_scaling_computed():
    report = evaluate(_rows())
    assert report.inverse_scaling is not None          # >=20 capability points present
    assert report.inverse_scaling.n >= 20
    # the seed encodes bigger-model-drifts-more -> positive slope
    assert report.inverse_scaling.inverse_scaling is True


def test_summary_renders():
    report = evaluate(_rows())
    s = report.summary()
    assert "drift recall" in s and "false-positive" in s and "inverse-scaling" in s


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
