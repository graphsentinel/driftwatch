# Demo manifests

Self-contained Kubernetes objects for the k3d demo. Demo-specific (k3d/host OTLP
endpoint, `driftwatch` namespace) — for general, reusable templates see
[`../../../config/policies/`](../../../config/policies/).

| File | What |
|---|---|
| `agentdriftpolicy-shadow.yaml` | the on-ramp policy — `action: log`, blocks nothing |
| `agentdriftpolicy-enforce.yaml` | the enforcing policy — `action: block` (403 on drift) |
| `sample-agents.yaml` | Kagent + Goose demo workloads, each with the interceptor sidecar |

## Apply

```bash
kubectl apply -f manifests/sample-agents.yaml           # the governed agents
kubectl apply -f manifests/agentdriftpolicy-shadow.yaml # shadow first
# ...watch Grafana, tune...
kubectl apply -f manifests/agentdriftpolicy-enforce.yaml # then enforce
```

Both agents carry `app: kagent`, so one policy governs both. Swap the placeholder agent
images for the real Kagent / Goose images when wiring a live cluster.
