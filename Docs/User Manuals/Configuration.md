# DriftWatch — Configuration Reference

Every configuration surface: Helm values, environment variables, the `AgentDriftPolicy` CRD, the
`AgenticArchitecture` CRD (declared contract), the prompt-aware cross-check, and the central
multi-app model. Install: **[Installation.md](Installation.md)**. Failures:
**[Troubleshooting.md](Troubleshooting.md)**.

How config flows: the **operator** reconciles a CRD → writes the baseline + policy knobs to the PVC
and delivers policy via **env** to the data plane (FR-10). So the same knobs appear as CRD fields,
Helm values, and env vars depending on the path.

---

## 1. Helm values

| Value | Default | Meaning |
|---|---|---|
| `image.repository` | `ghcr.io/graphsentinel/driftwatch` | image repo |
| `image.tag` | `0.1.0a0` | tag |
| `image.digest` | `""` | pin `sha256:…` for reproducible installs (overrides tag) |
| `image.pullPolicy` | `IfNotPresent` | |
| `securityContext.*` | hardened | non-root uid 10001, readOnlyRootFilesystem, drop ALL caps |
| `operator.replicas` | `1` | operator replicas |
| `operator.resources` | 50m/128Mi → 250m/256Mi | |
| `interceptor.port` | `8080` | sidecar HTTP port |
| `interceptor.resources` | 25m/64Mi → 100m/128Mi | |
| `otel.endpoint` | `otel-collector.observability.svc:4317` | OTLP target (decoupled per env) |
| `otel.protocol` | `grpc` | `grpc` \| `http/protobuf` |
| `crd.install` | `true` | install the CRDs with the chart |
| `persistence.enabled` | `false` | durable baseline PVC (required for MCP proxy; recommended for prod) |
| `persistence.size` | `1Gi` | |
| `persistence.storageClassName` | `""` | `""` = cluster default |
| `persistence.accessModes` | `[ReadWriteOnce]` | |
| `rbac.create` | `true` | create Role/ClusterRole + binding |
| `rbac.namespaced` | `false` | `true` = namespace-scoped Role (least-privilege) instead of ClusterRole |
| `webhook.enabled` | `false` | sidecar-injector mutating webhook (roadmap; manual sidecar today) |
| `webhook.failurePolicy` | `Ignore` | so a webhook outage never blocks pod creation |
| `mcpProxy.*` | see §3 | chain-aware MCP proxy (path B) |

---

## 2. Environment variables (data plane)

The operator sets these; you can also set them by hand for local runs.

| Var | Default | Meaning |
|---|---|---|
| `DRIFTWATCH_PORT` | `8080` (interceptor) / `8000` (mcp) | port `run()` binds |
| `DRIFTWATCH_HOST` | `0.0.0.0` | bind host |
| `DRIFTWATCH_OTLP_ENDPOINT` | — | OTLP target for `gen_ai.agent.*` emission |
| `DRIFTWATCH_DATA_DIR` | `data` | shared store path (baseline + `contracts/<ref>.json`) |
| `DRIFTWATCH_ACTION` | `block` | `log` \| `drop` \| `block` |
| `DRIFTWATCH_THRESHOLD` | `3.0` | raw z-score at which a call counts as drift |
| `DRIFTWATCH_WINDOW` | `50` | rolling window (runs) per task type |
| `DRIFTWATCH_FEATURES` | all 4 | comma list of `tool,scope,sequence,argSchemaHash` |
| `DRIFTWATCH_FAILURE_POLICY` | `failClosed` | `failClosed` (block on cold/error) \| `failOpen` (forward) |
| `DRIFTWATCH_TASK_TYPE` | `""` | scope per-session chains to one task type (proxy) |
| `DRIFTWATCH_UPSTREAM_MCP` | `""` | single upstream MCP ToolServer URL (proxy) |
| `DRIFTWATCH_UPSTREAMS` | `""` | cross-server: `name=url,name=url` (takes precedence) |
| `DRIFTWATCH_CONTRACT_REF` | `""` | declared contract name to load at startup (single-app) |
| `DRIFTWATCH_AGENT_ID` | `""` | which agent this seat fronts (fallback when no `_meta.agent`) |
| `DRIFTWATCH_CROSS_CHECK_ENABLED` | `false` | enable the shadow cross-check |
| `DRIFTWATCH_CROSS_CHECK_PROVIDER` | `ollama` | `ollama`\|`openai`\|`anthropic`\|`gemini`\|`bedrock`\|`runpod` |
| `DRIFTWATCH_CROSS_CHECK_MODEL` | `""` | cross-check model id |
| `DRIFTWATCH_CROSS_CHECK_ENDPOINT` | `""` | base_url (openai-compatible/runpod) / host (ollama) |
| `DRIFTWATCH_CROSS_CHECK_VOTES` | `1` | N-vote majority |
| `DRIFTWATCH_CROSS_CHECK_TIMEOUT` | `90` | total cross-check deadline (s) |
| `DRIFTWATCH_MCP_STRICT` | `false` | strict upstream wiring (fail rather than degrade) |
| `<PROVIDER>_API_KEY` | — | cross-check LLM key by standard name (via `crossCheck.apiKeySecretRef`) |

---

## 3. `mcpProxy.*` (chain-aware MCP proxy, path B)

| Value | Default | Meaning |
|---|---|---|
| `mcpProxy.enabled` | `false` | enable the proxy (Deployment + Service) |
| `mcpProxy.port` | `8000` | proxy port (sets `DRIFTWATCH_PORT`) |
| `mcpProxy.upstreamMcp` | `""` | single upstream MCP ToolServer URL |
| `mcpProxy.upstreams` | `[]` | cross-server list `{name,url}` (precedence over `upstreamMcp`); names unique, no `_` |
| `mcpProxy.taskType` | `""` | scope chains to one task type |
| `mcpProxy.contractRef` | `""` | declared contract name (E11) the operator reconciled |
| `mcpProxy.agentId` | `""` | agent the seat fronts (fallback; multi-app uses `_meta.agent`) |
| `mcpProxy.persistContracts` | `false` | mount PVC **writable** so pushed contracts survive restart (else in-memory) |
| `mcpProxy.action` | `block` | `log`\|`drop`\|`block` |
| `mcpProxy.threshold` | `"3.0"` | drift z-score threshold |
| `mcpProxy.failurePolicy` | `failClosed` | posture on cold/error |
| `mcpProxy.features` | `tool,scope,sequence,argSchemaHash` | decision-chain dimensions |
| `mcpProxy.crossCheck.enabled` | `false` | prompt-aware shadow cross-check |
| `mcpProxy.crossCheck.provider` | `ollama` | LLM provider |
| `mcpProxy.crossCheck.model` | `""` | model id |
| `mcpProxy.crossCheck.endpoint` | `""` | base_url / host |
| `mcpProxy.crossCheck.apiKeySecretRef.{name,key,envName}` | `"",apiKey,OPENAI_API_KEY` | LLM key from a Secret (never plaintext) |
| `mcpProxy.resources` | 25m/96Mi → 200m/256Mi | |

> The proxy **requires** `persistence.enabled=true` (mounts the operator baseline read-only) and one
> of `upstreamMcp` / `upstreams`.

---

## 4. `AgentDriftPolicy` CRD

The governance contract as one declarative object (operator reconciles `spec`, writes `status`):

```yaml
apiVersion: driftwatch.graphsentinel.org/v1alpha1
kind: AgentDriftPolicy
metadata: { name: kagent-cluster-ops }
spec:
  selector:
    matchLabels: { app: kagent }          # which agent workloads this governs
  runtimes:                                # declared, not hard-coded — adapter per runtime
    - { name: kagent, adapter: builtin/kagent }
    - { name: goose,  adapter: builtin/goose }
    - name: my-agent
      adapter: custom
      interceptor: { port: 8080, protocol: mcp }   # mcp | openai-tools | http
  baseline:
    sources: [approvedTraces, successfulRuns, dryRun]   # what "normal" is learned from
    # - models: [qwen, gemma]            # OPTIONAL cold-start seed (needs GPU); omit = pure stats
    aggregate: mean
    window: 50                            # rolling N runs per task type
  detection:
    features: [tool, scope, sequence, argSchemaHash]
    algorithm: streaming-zscore
    threshold: 3.0                        # raw z-score
  action: block                           # log | drop | block
  failurePolicy: failClosed               # failClosed | failOpen
  crossCheck:                             # E13 §4c prompt-aware shadow (optional)
    enabled: false
    provider: ollama
    model: ""
    endpoint: ""
    apiKeySecretRef: { name: "", key: apiKey, envName: OPENAI_API_KEY }
  observability:
    otel:
      emitEvaluationOn: [baseline_deviation, danger_detected]
      endpoint: "${DRIFTWATCH_OTLP_ENDPOINT:-otel-collector:4317}"
      protocol: grpc
status:                                   # operator-written (you never set it)
  observedTaskTypes: 6
  baselineReady: true
```

**Detection features** (each trips a different drift class):
`tool` (substitution), `scope` (escalation), `sequence` (wrong order / n-gram), `argSchemaHash`
(structurally novel arguments). **Statistics by default**; add `baseline.sources[].models` only if
you have GPU for a sharper cold-start.

---

## 5. `AgenticArchitecture` CRD (declared contract, E11/E12)

Reconciled into a declared contract the data plane checks **before** the statistical baseline. Same
shape AgentGate generates from. Key fields used by governance: `tools` (risk_map), per-agent
`tools`/`scope`/`canDelegateTo`, and `rules` (deny-sequences). It is persisted as
`<DATA_DIR>/contracts/<name>.json`; reference it via `mcpProxy.contractRef` / `DRIFTWATCH_CONTRACT_REF`.
A call to an unbound tool / out-of-scope / forbidden sequence is a **declared violation** (blocked
regardless of baseline readiness). No contract → pure statistical drift (standalone).

---

## 6. Multi-app (central DriftWatch, N AgentGates)

The proxy keeps a registry `{app_ref → contract}` and routes each tool call by its `_meta.app`:
- Each AgentGate pushes its contract under its own `ref` (its app id) to `POST /contracts`.
- Every tool call carries `_meta.app` (= same id) and `_meta.agent`; the proxy selects the matching
  contract and checks that agent.
- **Routing contract:** if `_meta.app` is present it **must** match a registered contract, else the
  call is blocked as `unknown_app`. No `_meta.app` → the single/default contract (legacy/sidecar).
- `ref` is whitelist-validated (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`); a bad ref → `400`.
- `persistContracts=true` makes pushes survive a restart.

Full design: `Docs/design/e13-multi-app-design.md` (internal).

---

## 7. Cross-check providers (shadow, optional)

A light LLM predicts the expected tool from the agent's prompt (carried in `_meta`); divergence emits
a `danger_detected` signal. **Emit-only — never changes the verdict.** Same provider matrix as the
table in §2/§3; key via `apiKeySecretRef` (standard env name, never plaintext). Only the
forward-path runs it, bounded by `DRIFTWATCH_CROSS_CHECK_TIMEOUT`.

---

## 8. Telemetry (`gen_ai.agent.*`)

Every score, anomaly, and gate decision is emitted as the OpenTelemetry `gen_ai.agent.*` semconv
(spans: `baseline.*`, `computed.anomaly.*`, `gate.*`; event: `gen_ai.evaluation.result`). The
backend is decoupled — DriftWatch only pushes OTLP to `otel.endpoint`; run Jaeger/Prometheus/Grafana
in-cluster, compose, or cloud without changing what the cluster enforces.
