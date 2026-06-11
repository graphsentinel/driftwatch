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

That MCP tool-call hop *is* the decision plane DriftWatch governs. Register DriftWatch as an
**MCP proxy** (point Kagent's `RemoteMCPServer` at it); it scores each
`(tool, scope, argSchemaHash)` and `log`/`drop`/`block`s before the call reaches the real
ToolServer. The chain-aware MCP proxy is implemented (E7) and validated in-cluster against a
real Kubernetes MCP ToolServer (E8); driving it from a real Kagent client (E9) is the gated
next step — see `e7-kagent-e2e.sh`.

### Choosing the model provider (Kagent)

The agent needs an LLM. **DriftWatch is provider-agnostic** — it governs the tool-selection
chain, not which model produced it — so you pick whatever provider you run. `make kagent-model`
(wrapping `setup-kagent-model.sh`) configures Kagent's `ModelConfig` + an API-key Secret; **keys
are read from your environment and never written into a script.**

| Provider | Command | Support |
|---|---|---|
| **Ollama** (default) | `make kagent-model MODEL_PROVIDER=ollama` | **verified** — key-free; auto-wires `host.k3d.internal` |
| OpenAI | `make kagent-model MODEL_PROVIDER=openai OPENAI_API_KEY=...` | scaffolded |
| Anthropic | `make kagent-model MODEL_PROVIDER=anthropic ANTHROPIC_API_KEY=...` | scaffolded |
| Gemini | `make kagent-model MODEL_PROVIDER=gemini GEMINI_API_KEY=...` | scaffolded |
| Azure OpenAI | `make kagent-model MODEL_PROVIDER=azure AZURE_API_KEY=... AZURE_ENDPOINT=...` | scaffolded |
| Bedrock | `make kagent-model MODEL_PROVIDER=bedrock AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...` | scaffolded |

Override the model name with `MODEL=<name>`. The agent's model **must support function calling**
(Kagent calls tools); e.g. `qwen3-coder-next:cloud` on Ollama does.

> **Two levels of honesty.** *Ollama* is the **verified** path used for this demo. The cloud
> providers are **scaffolded**: the script validates the key, creates the Secret, and emits a
> `ModelConfig` — but provider-specific fields track the Kagent chart version, so verify against
> your install (`kubectl explain modelconfig.spec`).

#### Where Ollama runs (network reachability)

The agent pod runs inside k3d; Ollama usually doesn't. `setup-kagent-model.sh` (provider
`ollama`) calls `register-host-alias.sh` for you, so manifests stay IP-free:

| Where Ollama runs | What to pass | Reach it as |
|---|---|---|
| **On the k3d host** (default) | nothing — host gateway discovered automatically | `http://host.k3d.internal:11434` |
| **On a separate server** | `OLLAMA_HOST=<ip-or-dns>` | `http://host.k3d.internal:11434` |
| **Inside the cluster** (self-contained) | deploy Ollama as a Service; `OLLAMA_BASE_URL=http://ollama.<ns>.svc:11434` | that Service URL |

> Ollama must listen on all interfaces (`OLLAMA_HOST=0.0.0.0` on the server), not just localhost,
> so the cluster can reach it.

> **Scope note:** the **core demo (path A, the five scenarios) needs none of this** — no model
> provider, no host alias, no Ollama. Provider setup + host alias exist **only** for E9 (real
> Kagent). Keeping them out of `cluster-up` keeps the core demo clean; `e7-kagent-e2e.sh` (step 0)
> or `make kagent-model` sets them up on demand.

## Tear down

```bash
make cluster-down
make obs-down
```

## Fallback

If the live cluster misbehaves on stage, the standalone `make demo-N` commands are the
safety net (they need only Python). Pre-recorded casts live under `recordings/`.
