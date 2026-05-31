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
| `src/driftwatch/graph/` | Neo4j decision-graph forensics — **roadmap/stub** (exporter not wired in v1alpha1) |
| `src/driftwatch/otel/` | `gen_ai.agent.*` emission (Observability Summit semconv) |
| `src/driftwatch/operator/` | Kopf control plane |
| `src/driftwatch/interceptor/` | data-plane sidecar |
| `config/` | general reference — `otel-targets.yaml` (the OTLP-endpoint / decoupling reference) |
| `deploy/` | Helm chart + raw CRD manifest + [install guide](deploy/README.md) |
| `evaluation/` | dataset + `make eval` harness (writes `results/`, git-ignored) |
| `examples/k3d-cluster-demo/` | self-contained demo — compose, k3d/collector/prometheus + `grafana/` provisioning, `manifests/` (policies + stand-in agents), `SETUP_RUNBOOK.md`, own `Makefile` |
| `tests/` · `Docs/` | functional suite (TC-F-*) · architecture, adapter guide, FP-tuning, GHCR publishing, consensus/MCP-proxy plan |

## Quick start

```bash
make install          # editable install with dev extras
make test             # run the functional suite (TC-F-*)
make eval             # run the drift dataset → recall / FP-rate / p95
```

Project-wide targets live in the root [Makefile](Makefile) (`install`/`test`/`lint`/`eval`/`clean`).
The k3d demo has its own [Makefile](examples/k3d-cluster-demo/Makefile)
(`cluster-up`/`obs-up`/`deploy`/`demo-1..5`) — run from the demo dir or via
`make -C examples/k3d-cluster-demo <target>`.

## Install (on a cluster)

Install the governance plane (CRD + operator + RBAC) from the published chart, then
drive it with a policy:

```bash
helm install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --namespace driftwatch --create-namespace \
  --set otel.endpoint=host.k3d.internal:4317
kubectl apply -f examples/k3d-cluster-demo/manifests/agentdriftpolicy-shadow.yaml  # shadow first, then enforce
```

Full install, policy, sidecar, values, and troubleshooting → **[deploy/README.md](deploy/README.md)**.
Publishing the image + chart to GHCR (maintainer) → **[Docs/publishing-ghcr.md](Docs/publishing-ghcr.md)**.

## Status

`v1alpha1` — Python/Kopf reference implementation, **validated end-to-end on k3d via
path A** (deterministic stand-in workloads + in-process demo): operator reconciles the
policy and writes status, telemetry lands in Jaeger/Prometheus/Grafana. **Planned next:**
pushing the reconciled baseline into the live sidecar (FR-10 / RE1 handoff), governing a
real Helm-installed Kagent at the MCP hop (path B / E7), and production hardening
(CI runtime coverage, securityContext, persistence) — see
[Docs/review-remediation-plan.md](Docs/review-remediation-plan.md). A Go/controller-runtime
rewrite is on the roadmap once the CRD contract stabilizes.

Apache-2.0.
