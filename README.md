# DriftWatch

**Govern what an AI agent actually does — catch tool-call drift before it reaches the API.**

DriftWatch sits in front of an agent's tools (as a chain-aware MCP proxy or a sidecar) and scores
every tool call against a **declared contract** and a **learned baseline**, then logs / drops /
blocks drift in real time. It governs the *hand* (tool calls), not the *brain* (the LLM) — so it is
framework- and model-agnostic.

```
agent ──▶ MCP tools/call ──▶ DriftWatch ──▶ [declared → baseline → cross-check] ──▶ upstream MCP
                                  │  log / drop / block  (gen_ai.agent.* spans → Jaeger/Prometheus)
```

## Three layers of governance
1. **Declared (deterministic)** — a contract reconciled from an `AgentDriftPolicy` /
   `AgenticArchitecture`: per-call bound-tool + scope check, forbidden deny-sequences, runtime
   delegation check (novel-edge / cycle / scope-escalation). Known-bad, prompt-independent.
2. **Statistical baseline (learned)** — a per-`task_type` rolling-window model (fingerprint, n-gram,
   z-score) flags baseline_mismatch / scope_creep / blocked_transition / arg_schema_novel /
   risk_escalation. Cold-start seeded by an FR-9 model-panel consensus.
3. **LLM cross-check (prompt-aware, shadow)** — a light LLM predicts the expected tool from the
   prompt (carried over MCP `_meta`); divergence → `danger_detected`. Emit-only; hard blocks stay on
   the declared layer.

## Install (Helm)
```bash
helm install driftwatch deploy/helm/driftwatch -n driftwatch --create-namespace
# components: driftwatch-operator (reconcile), driftwatch-interceptor (sidecar :8080),
#             driftwatch-mcp (chain-aware MCP proxy :8000, the .[mcp] extra)
```
Apply a policy + an org, and the operator reconciles a contract + baseline into the data plane:
```bash
kubectl apply -f deploy/crd/agentdriftpolicy.yaml
kubectl apply -f deploy/crd/agenticarchitecture.yaml
```

## Demo (no cluster needed)
```bash
pip install -e ".[dev]"
driftwatch demo tool_substitution     # one of: tool_substitution scope_escalation
driftwatch demo sequence_inversion    #         sequence_inversion argument_injection retry_storm
```
Each scenario runs the real detection core against a scripted chain and prints the verdict
(anomaly.kind, score, gate.action). The full observability stack (OTel Collector + Jaeger +
Prometheus + Grafana) is in `examples/k3d-cluster-demo/`.

## Configuration (env)
- `DRIFTWATCH_ACTION` = block | drop | log · `DRIFTWATCH_THRESHOLD` (z, default 3.0)
- `DRIFTWATCH_FAILURE_POLICY` = failClosed | failOpen · `DRIFTWATCH_OTLP_ENDPOINT` (host:4317)
- `DRIFTWATCH_CONTRACT_REF` (declared layer) · `DRIFTWATCH_TASK_TYPE` / `DRIFTWATCH_AGENT_ID`
- Cross-check (opt-in, shadow): `DRIFTWATCH_CROSS_CHECK_ENABLED`, `_MODEL`, `_ENDPOINT`, `_VOTES`,
  `_TIMEOUT`

## Reaching a host / remote LLM (and the collector) from the cluster (CoreDNS)

The OTLP collector (`host.k3d.internal:4317`) and a host-local **Ollama** (cross-check light LLM, or
baseline seeding) are reached from in-cluster pods by the **stable name** `host.k3d.internal` — no
hardcoded IP in any manifest. Register it once (idempotent CoreDNS `NodeHosts` patch + restart):

```bash
# local Ollama / collector on the k3d host (default)
./examples/k3d-cluster-demo/register-host-alias.sh driftwatch-demo

# remote Ollama by IP or DNS name
OLLAMA_HOST=10.0.0.42            ./examples/k3d-cluster-demo/register-host-alias.sh
OLLAMA_HOST=ollama.corp.example  ./examples/k3d-cluster-demo/register-host-alias.sh
```

Then `DRIFTWATCH_OTLP_ENDPOINT=host.k3d.internal:4317` and a cross-check Ollama `endpoint` resolve
from every pod. **Cloud cross-check providers** (openai-compatible / RunPod / anthropic / gemini /
bedrock) use their public endpoints — no alias needed. See `examples/k3d-cluster-demo/SETUP_RUNBOOK.md`.

## Interop with AgentGate
Separate repos, **no shared package** — only protocols: the `gen_ai.agent.*` semconv, the MCP `_meta`
cross-check (AgentGate writes the prompt, DriftWatch reads it), and the `AgenticArchitecture` format
(AgentGate generates an app from it; DriftWatch reconciles it into a governance contract). AgentGate
produces the action; DriftWatch governs it. Tool *selection* is always the agent's.

See `VISION.md` for the full picture.
