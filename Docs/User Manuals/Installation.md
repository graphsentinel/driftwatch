# DriftWatch — Installation Guide

DriftWatch governs an AI agent's **decision plane** — it scores each live tool/MCP call chain against
a learned per-task baseline **and** a declared contract, then logs/drops/blocks drift *before* the
call reaches the API. This guide covers every supported way to install and run it.

Config knobs: **[Configuration.md](Configuration.md)**. Problems: **[Troubleshooting.md](Troubleshooting.md)**.

---

## 1. Components & entrypoints

One image, three runtime roles (the `command:` picks the entrypoint):

| Entrypoint | Role | Port (default) | Needs |
|---|---|---|---|
| `driftwatch-operator` | reconciles `AgentDriftPolicy` / `AgenticArchitecture` CRDs into baseline + policy | — | `[operator]` |
| `driftwatch-interceptor` | per-pod sidecar HTTP enforcer (`/v1/tool-call`, `/contracts`, `/healthz`) | `8080` | `[interceptor]` |
| `driftwatch-mcp` | E7 path-B **chain-aware MCP proxy** in front of real MCP ToolServers (`/contracts`, `/healthz` + MCP) | `8000` | `[mcp]` (fastmcp) |
| `driftwatch` / `driftwatch-eval` | CLI: demo / eval / consensus-seed | — | |

**Control plane** = operator (reconcile). **Data plane** = interceptor sidecar *or* MCP proxy
(enforce). They share state via a PVC (the operator writes the baseline; the data plane reads it).

---

## 2. Prerequisites

| Need | For |
|---|---|
| Kubernetes (k3d / kind / any) | the operator + data plane |
| Helm ≥ 3.8 | OCI chart install |
| Podman or Docker | building / running the image locally |
| A real MCP ToolServer | only for the MCP-proxy (path B) |
| An OTLP collector (optional) | telemetry (`gen_ai.agent.*`) |
| An LLM endpoint (optional) | the prompt-aware cross-check (shadow) |

Artifacts (GHCR, owner `graphsentinel`):

| Artifact | Reference |
|---|---|
| Image (operator + interceptor + mcp) | `ghcr.io/graphsentinel/driftwatch:0.1.0a0` |
| Helm chart (OCI) | `oci://ghcr.io/graphsentinel/charts/driftwatch:0.1.0` |

---

## 3. Kubernetes (Helm) — operator + CRDs

```bash
helm install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  --namespace driftwatch --create-namespace \
  --set otel.endpoint=otel-collector.observability.svc:4317
```

This installs the CRDs (`AgentDriftPolicy`, `AgenticArchitecture`), the operator, and RBAC. Verify:

```bash
kubectl get crd agentdriftpolicies.driftwatch.graphsentinel.org
kubectl -n driftwatch get pods
```

Apply a policy — **shadow first, then enforce** (build trust before blocking):

```bash
kubectl apply -f .../agentdriftpolicy-shadow.yaml     # action: log
# ...watch OTel/Jaeger, tune threshold/window...
kubectl apply -f .../agentdriftpolicy-enforce.yaml    # action: block
```

Govern an agent pod via the **manual sidecar** (the webhook injector is roadmap):

```bash
kubectl apply -f deploy/sidecar-manual.yaml
```

---

## 4. Path B — chain-aware MCP proxy (real Kagent / MCP)

The MCP proxy fronts a real MCP ToolServer; the agent runtime points at the proxy instead of the
ToolServer. **Requires `persistence.enabled=true`** (the proxy mounts the operator-written baseline
read-only from the shared PVC).

```bash
helm upgrade --install driftwatch oci://ghcr.io/graphsentinel/charts/driftwatch --version 0.1.0 \
  -n driftwatch --create-namespace \
  --set persistence.enabled=true \
  --set mcpProxy.enabled=true \
  --set mcpProxy.upstreamMcp=http://k8s-mcp.driftwatch.svc:8080/mcp \
  --set mcpProxy.taskType=investigate_latency \
  --set mcpProxy.action=block
```

Point Kagent at it with a `RemoteMCPServer` CR (no change to the agent):

```yaml
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
metadata: { name: driftwatch-governed-tools, namespace: kagent }
spec:
  url: http://driftwatch-mcp.driftwatch.svc.cluster.local:8000/mcp
  protocol: STREAMABLE_HTTP
```

Cross-server (E10): set `mcpProxy.upstreams` (a list of `{name,url}`) instead of `upstreamMcp` to
front several ToolServers; tools surface as `<name>_<tool>` and cross-server transitions are scored.

---

## 5. Central multi-app proxy (N AgentGates → 1 DriftWatch)

The MCP proxy always exposes `POST /contracts` and routes each tool call by its `_meta.app` to that
app's declared contract. Several AgentGate apps can push and be governed independently. To let pushed
contracts **survive a proxy restart**, mount the PVC writable:

```bash
--set mcpProxy.persistContracts=true
```

Otherwise the registry is in-memory and apps re-push on restart. See
[Configuration.md](Configuration.md) §Multi-app and `Docs/design/e13-multi-app-design.md` (internal).

---

## 6. Local container (smoke / sidecar HTTP path)

```bash
# interceptor HTTP enforcer on a free port; failOpen so cold-start forwards during a smoke
podman run --rm -p 8081:8080 \
  -e DRIFTWATCH_PORT=8080 -e DRIFTWATCH_FAILURE_POLICY=failOpen \
  ghcr.io/graphsentinel/driftwatch:0.1.0a0 driftwatch-interceptor
curl -s localhost:8081/healthz
curl -s -X POST localhost:8081/v1/tool-call -H 'content-type: application/json' \
  -d '{"tool":"QueryLogs","namespace":"checkout"}'
```

---

## 7. Building the image yourself

```bash
podman build -t ghcr.io/graphsentinel/driftwatch:0.1.0a0 .
# publish: see Docs/design/publishing-ghcr.md (internal)
```

The image installs the `operator,interceptor,mcp` extras; the entrypoint is chosen by `command:`
(default `driftwatch-operator`).

---

## 8. Persistence & production notes

- **`persistence.enabled=true`** is required for the MCP proxy and recommended for prod so the
  baseline survives operator restarts (default off = per-pod emptyDir, demo only).
- The baseline PVC is **operator-writes / data-plane-reads** (read-only mount) by design. Multi-app
  contract persistence on the proxy opts into a writable mount via `mcpProxy.persistContracts`.
- `failurePolicy: failClosed` (default) blocks on a cold/unreachable baseline; `failOpen` forwards.
  Pick per risk tolerance.
