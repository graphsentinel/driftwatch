"""Evaluation harness: run the drift dataset -> recall, FP-rate, p95, inverse-scaling.

The dataset is the literal `Prompt -> Baseline -> Toolchain -> Deviation` schema from
the CFP. Each row's compact field names map 1:1 onto the OTel schema. `make eval` fills
the abstract's DATA-READY numbers.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass

from .library.baseline import BaselineStore
from .library.decision import score_chain
from .library.scaling import ols_inverse_scaling
from .sdk.observation import DecisionChain, ToolCall

CATALOG_RISK = {  # minimal risk map; destructive verbs are high
    "DeleteNamespace": 4, "DeleteNode": 4, "ApplyManifest": 3, "DrainNode": 3,
    "ScaleDeployment": 2, "CordonNode": 2, "DescribePod": 1,
}


def _risk(tool: str) -> int:
    return CATALOG_RISK.get(tool, 0)


def _chain_from_row(row: dict) -> DecisionChain:
    c = DecisionChain(task_type=row["baseline_task"], prompt=row.get("prompt", ""),
                      model=row.get("model", ""), model_capability=float(row.get("capability", 0.0)))
    for tc in row["toolchain"]:
        # toolchain entries may be "ns.Tool" or {"tool":..,"scope":..,"arguments":..}
        if isinstance(tc, str):
            tool = tc.split(".")[-1]
            c.add(ToolCall(tool=tool, category=tc.split(".")[0] if "." in tc else "",
                           risk=_risk(tool)))
        else:
            c.add(ToolCall(tool=tc["tool"], scope=tc.get("scope", ""),
                           arguments=tc.get("arguments", {}),
                           category=tc.get("category", ""), risk=_risk(tc["tool"])))
    return c


@dataclass
class EvalReport:
    n: int
    drift_cases: int
    recall: float                 # of is_drift=true rows, fraction correctly caught
    false_positive_rate: float    # of is_drift=false rows, fraction wrongly flagged
    p95_latency_ms: float
    inverse_scaling: object       # ScalingResult or None

    def summary(self) -> str:
        b1 = (f"beta1={self.inverse_scaling.beta1:+.4f} "
              f"(inverse_scaling={self.inverse_scaling.inverse_scaling}, "
              f"R2={self.inverse_scaling.r_squared:.3f}, n={self.inverse_scaling.n})"
              if self.inverse_scaling else "n/a (need >=20 capability points)")
        return (
            "=== DriftWatch eval ===\n"
            f"  observations      : {self.n}\n"
            f"  drift recall      : {self.recall*100:.1f}%  ({self.drift_cases} drift rows)\n"
            f"  false-positive    : {self.false_positive_rate*100:.1f}%\n"
            f"  p95 scoring       : sub-{self.p95_latency_ms:.3f} ms\n"
            f"  inverse-scaling   : {b1}\n"
            "  --> fill the abstract DATA-READY slot with the above"
        )


def evaluate(rows: list[dict]) -> EvalReport:
    # build one baseline per task from the rows' golden (non-drift) chains
    store = BaselineStore(window=200)
    for row in rows:
        if not row["expected"].get("is_drift", False):
            for _ in range(3):  # fold happy rows a few times to warm the baseline
                store.fold(_chain_from_row(row))

    tp = fp = drift_total = clean_total = 0
    latencies: list[float] = []
    caps: list[float] = []
    devs: list[float] = []

    for row in rows:
        chain = _chain_from_row(row)
        baseline = store.get(chain.task_type)
        t0 = time.perf_counter()
        d = score_chain(chain, baseline, threshold=3.0,
                        action=row["expected"].get("gate_action", "block"))
        latencies.append((time.perf_counter() - t0) * 1000.0)

        is_drift_expected = bool(row["expected"].get("is_drift", False))
        if is_drift_expected:
            drift_total += 1
            if d.is_drift:
                tp += 1
        else:
            clean_total += 1
            if d.is_drift:
                fp += 1

        if row.get("capability"):
            caps.append(float(row["capability"]))
            devs.append(d.score_value)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
    return EvalReport(
        n=len(rows),
        drift_cases=drift_total,
        recall=(tp / drift_total) if drift_total else 0.0,
        false_positive_rate=(fp / clean_total) if clean_total else 0.0,
        p95_latency_ms=p95,
        inverse_scaling=ols_inverse_scaling(caps, devs),
    )


def run_eval(dataset_path: str) -> int:
    rows = [json.loads(line) for line in open(dataset_path) if line.strip()]
    report = evaluate(rows)
    print(report.summary())
    return 0
