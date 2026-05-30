# Demo manifests

Self-contained Kubernetes objects for the k3d demo, and the project's single policy
set. Demo values are k3d-specific (host OTLP endpoint, `driftwatch` namespace) — copy
and adapt for your own cluster. The CRD field reference is
[`../../../deploy/crd/agentdriftpolicy.yaml`](../../../deploy/crd/agentdriftpolicy.yaml).

| File | What |
|---|---|
| `agentdriftpolicy-shadow.yaml` | the on-ramp policy — `action: log`, blocks nothing |
| `agentdriftpolicy-enforce.yaml` | the enforcing policy — `action: block` (403 on drift) |
| `sample-agents.yaml` | **stand-in** workloads (path A) with the interceptor sidecar — not real Kagent |

## Apply

```bash
kubectl apply -f manifests/sample-agents.yaml           # the governed agents
kubectl apply -f manifests/agentdriftpolicy-shadow.yaml # shadow first
# ...watch Grafana, tune...
kubectl apply -f manifests/agentdriftpolicy-enforce.yaml # then enforce
```

Both stand-ins carry `app: kagent`, so one policy governs both. These are **not** real
Kagent (which is Helm-installed and controller-managed) — for the Helm-based path B
where DriftWatch intercepts at the MCP tool-call hop, see the parent
[README](../README.md#running-against-real-kagent--goose-path-b).
