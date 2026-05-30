# DriftWatch — k3d live demo

The five drift scenarios from the talk, reproducible end-to-end. Governance runs in a
k3d cluster; the observability stack (Jaeger / Prometheus / Grafana / Neo4j) runs in
podman-compose on the host. They are decoupled — DriftWatch only pushes OTLP to
`host.k3d.internal:4317`.

## One-time

```bash
make install            # editable install into your env
```

## Bring it up

```bash
make obs-up             # podman-compose: OTel Collector + Jaeger + Prometheus + Grafana (+ Neo4j)
make cluster-up         # k3d cluster (driftwatch-demo)
make deploy             # helm install DriftWatch with values-k3d.yaml
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
kubectl apply -f manifests/sample-agents.yaml             # Kagent + Goose, each with the sidecar
kubectl apply -f manifests/agentdriftpolicy-shadow.yaml   # shadow on-ramp (action: log) — NFR-5
# ...watch Grafana, tune window/threshold...
kubectl apply -f manifests/agentdriftpolicy-enforce.yaml  # enforce (action: block) once trusted
```

In shadow nothing is blocked, but every would-have-blocked decision shows up in OTel;
enforce stops drift with a 403 before kube-apiserver. These manifests are the policy
set — copy and adapt them for your own cluster (the CRD field reference is
[`../../deploy/crd/agentdriftpolicy.yaml`](../../deploy/crd/agentdriftpolicy.yaml)).

## Tear down

```bash
make cluster-down
make obs-down
```

## Fallback

If the live cluster misbehaves on stage, the standalone `make demo-N` commands are the
safety net (they need only Python). Pre-recorded casts live under `recordings/`.
