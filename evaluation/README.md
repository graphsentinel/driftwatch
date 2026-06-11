# Evaluation

The dataset + harness behind every number in the abstract. Run:

```bash
make eval        # -> recall, false-positive rate, p95 latency, inverse-scaling
```

## Dataset — `datasets/drift.jsonl`

One JSONL row per observation, the literal **Prompt → Baseline → Toolchain → Deviation**
shape. Compact field names map 1:1 onto the OTel schema:

| dataset field | OTel |
|---|---|
| `score_value` | `gen_ai.evaluation.score.value` |
| `anomaly_kind` | `gen_ai.agent.computed.anomaly.kind` |
| `gate_action` | `gen_ai.agent.gate.action` |
| `is_drift` | drives recall (true) vs false-positive rate (false) |
| `model` / `capability` / `ambiguity` | reproduce the inverse-scaling trend |

- **Drift rows** (`is_drift: true`) measure **recall** — did the right anomaly trip?
- **Happy rows** (`is_drift: false`) anchor the baseline and measure the **FP rate**.
- `capability` (model size in B) drives the OLS `inverse_scaling_trend`.

## Seed vs real numbers

The committed `drift.jsonl` is a **synthetic seed**: drift is cleanly separable, so
recall ≈ 100% and FP ≈ 0%. These are *harness-validation* numbers, not the talk's
headline. The abstract's `<X>% / <FP>% / sub-<P>ms / β₁` come from running the same
harness over **real chains captured from a k3d cluster** (Kagent + Goose across model
tiers). Replace `drift.jsonl` with captured rows and re-run `make eval`.
