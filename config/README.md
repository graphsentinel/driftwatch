# config/

**General, reusable reference templates** — not tied to any one deployment or to the
demo. Nothing here is mounted or executed automatically; these are the starting points
you copy/adapt, and the canonical references the docs point at.

> Demo-specific, ready-to-run versions live in
> [`../examples/k3d-cluster-demo/`](../examples/k3d-cluster-demo/) (k3d endpoint, the
> `driftwatch` namespace, the compose-mounted collector/prometheus). This directory is
> the *template* layer; `examples/` is the *runnable* layer.

| File | What | Used by |
|---|---|---|
| `otel-targets.yaml` | The decoupling reference — **where DriftWatch pushes OTLP** (one endpoint; backend in-cluster / podman-compose / cloud). The single place that documents "telemetry destination is config, not code." | Referenced by `deploy/README.md`, `deploy/sidecar-manual.yaml`, and the demo's `k3d-config.yaml` / `otel-collector.yaml` as the canonical explanation. |
| `policies/shadow-mode.yaml` | A general `AgentDriftPolicy` template — `action: log` (the safe on-ramp). Env-var OTLP endpoint, no namespace baked in. | Copy + adapt for your cluster. The demo's k3d version is `examples/k3d-cluster-demo/manifests/agentdriftpolicy-shadow.yaml`. |
| `policies/kagent-cluster-ops.yaml` | A general template — `action: block` (enforce). | Same; demo version: `…/manifests/agentdriftpolicy-enforce.yaml`. |

## Why a separate template layer?

The same shadow→enforce policy appears twice on purpose:

- **here** = generic (portable endpoint, no namespace) → for *your* cluster;
- **in `examples/`** = concrete (k3d endpoint, `driftwatch` ns) → runs the demo as-is.

Keeping the generic templates out of `examples/` means a real adopter starts from a
clean, portable policy rather than reverse-engineering demo specifics.
