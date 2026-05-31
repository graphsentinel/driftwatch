# E7 real-upstream plan — DriftWatch in front of a real Kubernetes MCP ToolServer

**Goal:** prove E7 path-B against a *real* Kubernetes-facing MCP ToolServer (not a toy), in
two staged phases so each risk is isolated. The upstream is the community
**`containers/kubernetes-mcp-server`** (Go-native, talks to the K8s API directly; Streamable
HTTP at `/mcp`; has `--read-only` / `--disable-destructive`; reads in-cluster ServiceAccount;
Helm chart at `oci://ghcr.io/containers/charts/kubernetes-mcp-server`).

```
Target architecture (final):
  Kagent agent  →  DriftWatch MCP proxy  →  kubernetes-mcp-server  →  Kubernetes API
                   (chain-aware scoring,        (real K8s tools)
                    block before it lands)
```

**Why this is strong, not a toy:** DriftWatch's role is exactly its thesis — *not* writing a
tool server, but putting chain-aware governance in front of a real Kubernetes-facing MCP
ToolServer, so a drifting/destructive tool-call chain is scored and stopped **before it
reaches the K8s API**.

**Safety first:** the upstream runs **read-only / non-destructive** for the demo. Destructive
calls are exercised only as *block* scenarios — DriftWatch denies them and we prove the
upstream never received them (so even if the policy missed, read-only upstream is a second
guard).

---

## Phase 1 — minimal path-B proof (no Kagent, no model key)

```
FastMCP Client  →  DriftWatch MCP proxy  →  kubernetes-mcp-server  →  k3d API
```

Proves the *governance path* independently of any agent runtime. This is the fast, real
proof: a genuine Kubernetes MCP upstream, DriftWatch actually proxying in between.

### Tasks
- **T1.1 — Build + load the mcp-extra image.** `podman build` (Dockerfile now installs
  `[operator,interceptor,mcp]`), then `k3d image import` into `k3d-driftwatch-demo` (no
  registry push needed for local). Pin the tag the chart will use.
- **T1.2 — Deploy the upstream Kubernetes MCP ToolServer.** Install
  `kubernetes-mcp-server` in k3d (its Helm chart or a small Deployment+Service), **read-only**,
  Streamable HTTP on a port, with a ServiceAccount whose RBAC is read-only (get/list/watch).
  Confirm `tools/list` over `/mcp` works from inside the cluster.
- **T1.3 — Point the DriftWatch proxy at it.** `helm upgrade` with
  `mcpProxy.enabled=true`, `persistence.enabled=true`,
  `mcpProxy.upstreamMcp=http://<svc>.<ns>.svc.cluster.local:<port>/mcp`,
  image = the mcp-extra build. Proxy pod healthy, loads the baseline read-only from the PVC.
- **T1.4 — Seed a baseline from a normal K8s management chain.** e.g.
  `list_api_resources → get_resource → describe_resource` (the real upstream's tool names —
  confirm them via `tools/list`), folded as a trusted/consensus seed onto the shared PVC.
- **T1.5 — E2E with a FastMCP Client against the proxy Service:**
  - a within-baseline tool call → forwarded → upstream returns real K8s data;
  - a drift/destructive call → DriftWatch MCP error, **upstream never receives it**;
  - a `gen_ai.evaluation.result` with `gate.action=block` + the anomaly kind is emitted.

### Phase-1 acceptance criteria
- [ ] upstream `kubernetes-mcp-server` answers `tools/list` over `/mcp` in-cluster;
- [ ] DriftWatch proxy forwards a within-baseline call and relays the real upstream result;
- [ ] a destructive/drift call is blocked at the proxy; upstream logs/behavior show it was
      never called; OTel carries `gate.action=block`;
- [ ] path A (`make demo`) still green — E7 adds a path, doesn't replace the fallback.

---

## Phase 2 — real Kagent as the client

```
Kagent agent  →  DriftWatch MCP proxy (RemoteMCPServer)  →  kubernetes-mcp-server  →  k3d API
```

Only the **client** changes — swap the FastMCP test client for a real Kagent agent reaching
the proxy via a `RemoteMCPServer` CR. The governance path (proxy → upstream) is unchanged and
already proven in Phase 1, so any failure here is isolated to agent-runtime integration.

### Tasks
- **T2.1 — Install Kagent** (Helm) + a model provider Secret (OpenAI/etc.). Verify the
  controller comes up and an `Agent` CR creates an agent pod.
- **T2.2 — RemoteMCPServer → the DriftWatch proxy** (`examples/.../remotemcpserver.yaml`,
  fields verified against the installed Kagent CRD version).
- **T2.3 — Drive the agent**: a within-baseline task executes via the real upstream; a drift
  task is blocked at the MCP hop; confirm the upstream never received it and OTel shows the
  denial. Validate **drop-on-retry** behavior (the `"dropped …"` MCP error must not cause a
  retry loop — T-E7.5 acceptance check).

### Phase-2 acceptance criteria
- [ ] Kagent reaches the upstream's tools *through* the DriftWatch proxy (`tools/list`
      passthrough);
- [ ] a within-baseline agent task reaches `kubernetes-mcp-server` and returns;
- [ ] a drift/destructive agent task is denied at the proxy; upstream never called;
- [ ] no retry storm on a dropped call.

---

## Why staged (debuggability)

Doing both at once means a failure could be Kagent, the model provider, the RemoteMCPServer
CRD, the upstream MCP server, the DriftWatch proxy, or the baseline. Phase 1 nails down
"DriftWatch proxy + real Kubernetes MCP upstream works"; Phase 2 then only has to answer
"can Kagent use that proxy as a RemoteMCPServer". One variable at a time.

## What's already done (cluster-free, shipped)
- DriftWatch MCP proxy + middleware + mapping, fastmcp 3.3.1, 8 unit tests (TC-F-16/17,
  drop-as-error, per-session chain) — `tests/test_mcp_proxy.py`.
- Helm `mcpProxy` block + `templates/mcp-proxy.yaml` (Deployment+Service, read-only baseline
  mount, two render guards), `driftwatch-mcp` entrypoint, Dockerfile `mcp` extra.
- `examples/k3d-cluster-demo/e7-kagent-e2e.sh` skeleton + `remotemcpserver.yaml` example.

This plan is the bridge from those shipped pieces to a live, real-upstream demo.
