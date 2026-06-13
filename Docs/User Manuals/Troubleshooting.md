# DriftWatch — Troubleshooting

Concepts and concrete fixes. Pairs with **[Installation.md](Installation.md)** and
**[Configuration.md](Configuration.md)**.

## How to read DriftWatch's behavior first
- **Two layers, in order.** A call is first checked against the **declared contract** (deterministic,
  known-bad → `declared_violation` / `declared_sequence` / `unknown_app`); only if it passes is it
  scored against the **statistical baseline** (learned, unknown-bad). A block can come from either.
- **failurePolicy.** On a cold/not-ready baseline or an interceptor error, `failClosed` (default)
  **blocks** and `failOpen` **forwards**. Many "it blocks everything" reports are just cold-start
  failClosed.
- **Verdict vocabulary.** `forward` (200), `drop` (200, silent), `block` (403). At the MCP hop both
  drop and block surface as an MCP `ToolError` (the upstream is never called).

---

## Install / cluster

### MCP proxy won't start: requires upstream / requires persistence
`mcpProxy.enabled=true` needs **either** `mcpProxy.upstreamMcp` **or** `mcpProxy.upstreams`, **and**
`persistence.enabled=true` (it mounts the operator baseline read-only). The chart fails the render
with a clear message if either is missing.

### `address already in use` on :8000 or :8080
Another service holds the port (Harbor, k3d, a prior run). The proxy binds `DRIFTWATCH_PORT` (default
8000), the interceptor 8080. Map a free host port locally (`-p 8081:8080`) and set
`DRIFTWATCH_PORT` to match the container port; check the occupant with `ss -ltnp | grep :8000`.

### CRDs missing after install
`crd.install` must be `true` (default). Verify:
`kubectl get crd agentdriftpolicies.driftwatch.graphsentinel.org`. If you manage CRDs separately, set
`crd.install=false` and apply them yourself.

### Operator can't watch CRDs (RBAC)
CRD watch is cluster-scoped. With `rbac.namespaced=true` you get a namespace Role — but cluster CRD
**read** is still required; ensure the ClusterRole for CRD read exists. Check
`kubectl -n driftwatch logs deploy/driftwatch-operator`.

---

## Enforcement behaves unexpectedly

### It blocks every call (even normal ones)
Most likely **cold-start failClosed**: the baseline for that `task_type` isn't ready yet
(`status.baselineReady:false`, or the proxy's adapter `task_type` doesn't match a seeded baseline).
Options: run `action: log` (shadow) until the baseline fills; seed it (consensus-seed / trusted runs;
see `Docs/design/baseline-lifecycle-runbook.md`); or set `failurePolicy: failOpen` for non-critical
paths. Confirm the call's `task_type` matches a learned one.

### Declared violations block before the baseline is ready
By design — declared checks are deterministic and run first. If a tool is wrongly flagged "not bound
to agent X", the agent's `tools`/`scope` in the AgenticArchitecture is too narrow, or the wrong
`agent_id` is being used (see next).

### `unknown app '…'` blocks (multi-app)
A tool call's `_meta.app` names no registered contract. Causes: the AgentGate app's contract push
hasn't landed (DriftWatch was down at its startup), the app id differs between push `ref` and runtime
`_meta.app`, or the proxy restarted and `persistContracts=false` (in-memory registry lost). Fixes:
ensure the app re-pushes; keep the app id stable & unique; set `mcpProxy.persistContracts=true` to
survive restarts. A call with **no** `_meta.app` falls back to the default contract (legacy/sidecar).

### `/contracts` returns 400 `invalid ref`
The push `ref` must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` (no `/`, `..`, spaces, path
separators) — it becomes a filename. Fix the app id.

### The declared contract isn't being enforced at all
No contract is loaded. Single-app: set `mcpProxy.contractRef` / `DRIFTWATCH_CONTRACT_REF` to a name
the operator persisted under `<DATA_DIR>/contracts/<name>.json`. Multi-app: the app must push to
`/contracts` and stamp `_meta.app`. No contract → pure statistical drift (standalone), which is valid
but means no bound-tool/scope/sequence checks.

### Drift never fires even on obviously-wrong calls
Check `detection.features` includes the relevant dimension (`tool`/`scope`/`sequence`/`argSchemaHash`),
the `threshold` isn't too high, and the baseline `window` has enough real runs. A novel tool against
a too-small/empty baseline may also just cold-start.

### Too many false positives
Tune in this order: widen `window`, raise `threshold`, add `dryRun`/`approvedTraces` to
`baseline.sources`. See `Docs/design/fp-tuning-runbook.md`. Run `action: log` while tuning.

---

## MCP proxy / upstream

### `upstream error` / `Session terminated` mid-session
Real upstreams (e.g. kubernetes-mcp-server) drop idle Streamable-HTTP sessions. The proxy retries the
**session-fault class only** (E10 reconnect) with a fresh client; destructive verbs are not retried
by default (idempotency). A persistent error means the upstream URL is wrong/unreachable — verify
`mcpProxy.upstreamMcp` / each `upstreams[].url` resolves in-cluster.

### `no upstream for tool 'x_y'` (cross-server)
A namespaced tool's server segment doesn't match any `upstreams[].name`. Server names must be unique,
alphanumeric+`-`, and contain **no `_`** (so `<server>_<tool>` stays collision-free). Rename the
server.

### Tools don't appear in `tools/list`
The upstream was unreachable at mount. With `DRIFTWATCH_MCP_STRICT=true` this fails startup instead
of degrading. Check upstream reachability and the proxy logs.

---

## Persistence

### Pushed contracts vanish after a proxy restart
The registry is in-memory unless `mcpProxy.persistContracts=true` (which mounts the PVC writable so
`/data/contracts/*.json` persists and reloads via `load_all_contracts`). Otherwise apps must re-push
on restart.

### Best-effort persist silently does nothing
The baseline mount is read-only by default (operator-writes / proxy-reads invariant). A `/contracts`
push still works **in memory**; persistence is skipped on a read-only dir. Enable
`persistContracts` for a writable mount.

### Baseline lost after operator restart
`persistence.enabled=false` uses a per-pod emptyDir (demo). Set `persistence.enabled=true` for a
durable PVC so the baseline survives restarts.

---

## Cross-check (shadow)

### `cross-check SKIP … has_prompt=false`
The shadow cross-check needs the agent's prompt + candidate tools in the call's `_meta`. AgentGate
sends these only to **governed** (proxy) URLs. If the prompt is missing, the agent isn't routing
through the proxy with `_meta`, or cross-check is enabled on a non-governed path. It is emit-only and
never blocks, so this is a missing signal, not a failure.

### Cross-check adds latency
It runs only on the forward path, bounded by `DRIFTWATCH_CROSS_CHECK_TIMEOUT`. Lower the timeout or
votes, or disable it — it's a learned, additive flag, not enforcement.

---

## Telemetry

### No spans in Jaeger/Grafana
`DRIFTWATCH_OTLP_ENDPOINT` / `otel.endpoint` must point at a reachable collector. The backend is
decoupled (OTLP push only); from inside k3d use e.g. `host.k3d.internal:4317` or the in-cluster
collector Service. Check `observability.otel.emitEvaluationOn` includes the events you expect.
