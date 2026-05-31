"""Evaluation harness: run the drift dataset -> recall, FP-rate, p95, inverse-scaling.

The dataset is the literal `Prompt -> Baseline -> Toolchain -> Deviation` schema from
the CFP. Each row's compact field names map 1:1 onto the OTel schema. `make eval` fills
the abstract's DATA-READY numbers.
"""
from __future__ import annotations

import json
import time
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
    per_model: dict = None        # model -> {runs, blocked, matched, dev_sum, drift_*}
    rows: list = None             # per-observation drift rows (the jsonl payload)

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
            "  NOTE: seed-validation numbers — do NOT use for the CFP headline.\n"
            "        Replace datasets/drift.jsonl with real k3d-captured chains,\n"
            "        then these become the abstract's DATA-READY values."
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
    per_model: dict = {}
    drift_rows: list[dict] = []

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

        # per-model rollup (sre-incident-demo style) + one drift row per observation
        model = row.get("model", "unknown")
        m = per_model.setdefault(model, {
            "capability": float(row.get("capability", 0.0)),
            "runs": 0, "blocked": 0, "matched": 0, "dev_sum": 0.0,
            "drift_expected": 0, "drift_caught": 0, "drift_missed": 0,
        })
        m["runs"] += 1
        m["dev_sum"] += d.score_value
        if d.gate_blocked:
            m["blocked"] += 1
        if d.baseline_match:
            m["matched"] += 1
        if is_drift_expected:
            m["drift_expected"] += 1
            m["drift_caught" if d.is_drift else "drift_missed"] += 1

        drift_rows.append({
            "id": row.get("id"),
            "model": model,
            "capability": float(row.get("capability", 0.0)),
            "baseline_task": chain.task_type,
            "chain": chain.tools,
            "expected": row["expected"],
            "score_value": round(d.score_value, 4),
            "anomaly_kind": d.anomaly_kind,
            "gate_action": d.gate_action,
            "is_drift": d.is_drift,
            "correct": (d.is_drift == is_drift_expected),
        })

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
    return EvalReport(
        n=len(rows),
        drift_cases=drift_total,
        recall=(tp / drift_total) if drift_total else 0.0,
        false_positive_rate=(fp / clean_total) if clean_total else 0.0,
        p95_latency_ms=p95,
        inverse_scaling=ols_inverse_scaling(caps, devs),
        per_model=per_model,
        rows=drift_rows,
    )


def write_results(report: EvalReport, out_dir: str) -> list[str]:
    """Persist the run as three artifacts (sre-incident-demo layout):
      drift_inverse_scaling.json   — machine-readable rollup (per-model + headline)
      drift_inverse_scaling.txt    — human-readable summary table
      drift_rows.jsonl             — one row per scored observation
    Returns the paths written.
    """
    import os

    os.makedirs(out_dir, exist_ok=True)
    inv = report.inverse_scaling
    rollup = {
        "engine": "seed-validation",          # NOT real model runs — see NOTE in .txt
        "obs_total": report.n,
        "drift_cases": report.drift_cases,
        "recall": round(report.recall, 4),
        "false_positive_rate": round(report.false_positive_rate, 4),
        "p95_latency_ms": round(report.p95_latency_ms, 4),
        "inverse_scaling": (
            {"beta1": round(inv.beta1, 6), "is_inverse": inv.inverse_scaling,
             "r_squared": round(inv.r_squared, 4), "n": inv.n} if inv else None
        ),
        "per_model": {
            mdl: {**v, "avg_dev": round(v["dev_sum"] / v["runs"], 4) if v["runs"] else 0.0}
            for mdl, v in sorted(report.per_model.items(), key=lambda kv: kv[1]["capability"])
        },
    }
    json_path = os.path.join(out_dir, "drift_inverse_scaling.json")
    txt_path = os.path.join(out_dir, "drift_inverse_scaling.txt")
    jsonl_path = os.path.join(out_dir, "drift_rows.jsonl")

    with open(json_path, "w") as f:
        json.dump(rollup, f, indent=2)

    with open(jsonl_path, "w") as f:
        for r in report.rows:
            f.write(json.dumps(r) + "\n")

    lines = [
        "=" * 78,
        "  DriftWatch — drift detection + inverse-scaling (seed validation)",
        "=" * 78,
        f"  observations    : {report.n}",
        f"  drift recall    : {report.recall * 100:.1f}%  ({report.drift_cases} drift rows)",
        f"  false-positive  : {report.false_positive_rate * 100:.1f}%",
        f"  p95 scoring     : sub-{report.p95_latency_ms:.3f} ms",
        "",
        "  PER-MODEL (sorted by capability)",
        "  " + "-" * 74,
        f"  {'model':24} {'cap':>5} {'runs':>5} {'match%':>7} {'avgDev':>7} "
        f"{'drift':>6} {'caught':>7}",
    ]
    for mdl, v in rollup["per_model"].items():
        runs = v["runs"] or 1
        de = v["drift_expected"] or 0
        caught = (v["drift_caught"] / de * 100) if de else 0.0
        lines.append(
            f"  {mdl:24} {v['capability']:>5.1f} {v['runs']:>5} "
            f"{v['matched'] / runs * 100:>6.1f}% {v['avg_dev']:>7.3f} "
            f"{de:>6} {caught:>6.1f}%"
        )
    lines += ["  " + "-" * 74]
    if inv:
        lines.append(
            f"  inverse-scaling : beta1={inv.beta1:+.4f}  "
            f"(inverse={inv.inverse_scaling}, R2={inv.r_squared:.3f}, n={inv.n})")
    else:
        lines.append("  inverse-scaling : n/a (need >=20 capability points)")
    lines += [
        "",
        "  NOTE: SEED-VALIDATION numbers — do NOT use for the CFP headline.",
        "        The seed dataset exercises detection mechanics, not real model",
        "        behavior; beta1 here is not a real inverse-scaling result. Replace",
        "        evaluation/datasets/drift.jsonl with real k3d/ollama-captured chains",
        "        (pilot path B), then these become the abstract's DATA-READY values.",
        "",
    ]
    with open(txt_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    return [json_path, txt_path, jsonl_path]


def run_eval(dataset_path: str, out_dir: str | None = None) -> int:
    rows = [json.loads(line) for line in open(dataset_path) if line.strip()]
    report = evaluate(rows)
    print(report.summary())
    if out_dir:
        paths = write_results(report, out_dir)
        print("\n  wrote results:")
        for p in paths:
            print(f"    {p}")
    return 0
