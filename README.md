# DriftWatch

A Kubernetes operator that watches an AI agent's **decision plane** — its MCP and
tool calls — scores each live decision chain against a per-task baseline it learns
from real runs, and **logs / drops / blocks** drift *before it reaches the API*.
All declared in an `AgentDriftPolicy` CRD.

> Companion to the KubeCon NA 2026 proposal *"DriftWatch: Catching AI Agent Decision
> Drift Before It Hits the Kubernetes API"*. The full design lives in
> `../CFP-A — DriftWatch (alternative).md`.

## Why

Admission controllers see a well-formed API request — not *which* agent issued it,
*which* tool it picked, or *whether its decision chain has drifted*. By then the agent
has already decided. DriftWatch governs the layer admission can't see — complementary
to Kyverno/OPA, not a replacement.

## Layout

| Path | What |
|---|---|
| `src/driftwatch/library/` | shared detection core (fingerprint, n-gram, z-score, baseline, decision) |
| `src/driftwatch/sdk/` | stable contract for runtime-adapter authors |
| `src/driftwatch/adapters/` | built-in `kagent` / `goose` + custom example |
| `src/driftwatch/db/` | baseline persistence (memory / sqlite) |
| `src/driftwatch/graph/` | optional Neo4j decision-graph forensics |
| `src/driftwatch/otel/` | `gen_ai.agent.*` emission (Observability Summit semconv) |
| `src/driftwatch/operator/` | Kopf control plane |
| `src/driftwatch/interceptor/` | data-plane sidecar |
| `config/` | sample policies, OTel targets/collector, seed models |
| `deploy/` | Helm chart + raw CRD manifest |
| `evaluation/` | dataset + `make eval` harness |
| `examples/k3d-cluster-demo/` | the five live demo scenarios |

## Quick start

```bash
make install          # editable install with dev extras
make test             # run the functional suite (TC-F-*)
make eval             # run the drift dataset → recall / FP-rate / p95
```

See the [Makefile](Makefile) for cluster/demo targets.

## Status

`v1alpha1` — Python/Kopf reference implementation. A Go/controller-runtime rewrite
is on the roadmap once the CRD contract stabilizes.

Apache-2.0.
