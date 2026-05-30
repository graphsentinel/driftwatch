# DriftWatch — k3d live demo

The five drift scenarios from the talk, reproducible end-to-end. Governance runs in a
k3d cluster; the observability stack (Jaeger / Prometheus / Grafana) runs in
podman-compose on the host. They are decoupled — DriftWatch only pushes OTLP to
`host.k3d.internal:4317`.

> Neo4j decision-graph forensics is **roadmap**: a Neo4j service exists in
> `compose.yaml` behind a `forensics` profile (off by default), but the exporter
> (`src/driftwatch/graph/`) is a stub, so nothing is written to it yet.

> `make` targets below are this directory's [Makefile](Makefile). Project-wide targets
> (`install`, `test`, `lint`, `eval`) live in the root Makefile — run as
> `make -C ../.. <target>`.

## One-time

```bash
make -C ../.. install    # editable install into your env (root Makefile)
```

## Bring it up

```bash
make obs-up             # podman-compose: OTel Collector + Jaeger + Prometheus + Grafana
                        # (Neo4j is roadmap — opt in with: podman-compose --profile forensics up -d)
make cluster-up         # k3d cluster (driftwatch-demo)
make deploy             # helm install DriftWatch with values-k3d.yaml
# (or: make up   — all three at once)
```

- Grafana: http://localhost:3000  (dashboard: *DriftWatch — agent-decisions*)
- Jaeger:  http://localhost:16686

## The five scenarios

Each scores a real agent decision chain through the detection core and applies the
policy action. They run standalone (no cluster needed) so they are demo-safe:

```bash
make demo-1   # tool substitution   -> baseline_mismatch -> block
make demo-2   # scope escalation     -> scope_creep        -> block
make demo-3   # sequence inversion   -> blocked_transition -> drop
make demo-4   # argument injection   -> arg_schema_novel   -> block
make demo-5   # retry storm          -> baseline_mismatch  -> drop
```

In-cluster, the same chains flow through the injected interceptor sidecar; the
`gen_ai.agent.*` spans + `gen_ai.evaluation.result` events land in Grafana live.

## Apply the demo manifests (in-cluster)

The demo's own CRs live in [`manifests/`](manifests/) — self-contained, k3d-specific
(OTLP → `host.k3d.internal:4317`, `driftwatch` namespace):

```bash
kubectl apply -f manifests/sample-agents.yaml             # STAND-IN workloads (path A), each with the sidecar
kubectl apply -f manifests/agentdriftpolicy-shadow.yaml   # shadow on-ramp (action: log) — NFR-5
# ...watch Grafana, tune window/threshold...
kubectl apply -f manifests/agentdriftpolicy-enforce.yaml  # enforce (action: block) once trusted
```

In shadow nothing is blocked, but every would-have-blocked decision shows up in OTel;
enforce stops drift with a 403 before kube-apiserver. These manifests are the policy
set — copy and adapt them for your own cluster (the CRD field reference is
[`../../deploy/crd/agentdriftpolicy.yaml`](../../deploy/crd/agentdriftpolicy.yaml)).

## Running against real Kagent / Goose (path B)

What ships in `manifests/sample-agents.yaml` is a **stand-in** (path A): hand-authored
Deployments that emit deterministic tool-call traffic through the interceptor — the
reproducible fallback for the stage. It is **not** how real Kagent runs.

Real Kagent is **Helm-installed and controller-managed**: its controller creates an
agent pod per `Agent` CRD, and tool calls leave the agent pod over the network to
separate **MCP ToolServer** pods.

```bash
# Kagent (controller creates an agent pod per Agent CRD)
helm install kagent-crds oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds -n kagent --create-namespace
helm install kagent      oci://ghcr.io/kagent-dev/kagent/helm/kagent      -n kagent \
  --set providers.default=openAI --set providers.openAI.apiKey=$OPENAI_API_KEY
# Goose runs from the official image: ghcr.io/block/goose
```

That MCP tool-call hop *is* the decision plane DriftWatch governs. The integration point
is to register DriftWatch as an **MCP proxy** (point Kagent's `RemoteMCPServer` at it);
it scores each `(tool, scope, argSchemaHash)` and `log`/`drop`/`block`s before the call
reaches the real ToolServer.

> **Roadmap:** the DriftWatch **MCP-proxy adapter** for path B is the next sprint. Until
> it lands, the stand-in (path A) is the supported live-demo path and `make demo` is the
> in-process equivalent.

## Tear down

```bash
make cluster-down
make obs-down
```

## Fallback

If the live cluster misbehaves on stage, the standalone `make demo-N` commands are the
safety net (they need only Python). Pre-recorded casts live under `recordings/`.
