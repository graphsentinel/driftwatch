# Changelog

Sprint-by-sprint delivery history (see the CFP's Implementation Plan, S0–S6).
Each entry corresponds to one sprint commit.

## S0 — Scaffold
- Repo skeleton matching the CFP Repository Layout (`src/driftwatch/{library,sdk,adapters,db,graph,otel,operator,interceptor,crd}`, `config/`, `deploy/`, `evaluation/`, `examples/k3d-cluster-demo/`, `tests/`).
- `pyproject.toml` (package `driftwatch` v0.1.0a0, optional extras: operator/interceptor/graph/dev).
- `AgentDriftPolicy` CRD manifest (`deploy/crd/`) with OpenAPI v3 validation; `status` is a subresource (operator-written).
- Sample policies: `kagent-cluster-ops` (enforce) + `shadow-mode` (log) under `config/policies/`.
- `Makefile` (install/test/eval/cluster-up/obs-up/demo-1..5), `.gitignore`, CI workflow, README.

## S1 — Detection library
- `sdk/observation.py`: stable contract — `ToolCall` (with `arg_schema_hash` over key+type shape), `DecisionChain`, `RuntimeAdapter` base + registry (FR-8 foundation).
- `library/`: pure-statistics detection core shared by operator + interceptor —
  - `fingerprint.py` (tool, scope, argSchemaHash + category/risk),
  - `ngram.py` (tool-transition frequency + novel-transition detection),
  - `zscore.py` (streaming raw z-score + [0,1] normalized squash — the two-value model),
  - `baseline.py` (`TaskBaseline` + `BaselineStore`, rolling window, cold-start `ready` guard),
  - `decision.py` (four-feature scoring → anomaly.kind → gate.action; sequence drift attributed to the destination tool; vocabulary incl. additive `arg_schema_novel`),
  - `scaling.py` (OLS inverse-scaling, β₁>0 ⇒ bigger models drift more).
- `db/`: swappable persistence — `MemoryBackend` (dev/kind) + `SqliteBackend` (default, JSON-snapshot roundtrip).
- `tests/test_library_detection.py`: 9 green — TC-D-01/02/04/06, TC-F-04/13, z-score monotonicity, status-ready, sqlite roundtrip.
- Detection core adapted from Obs-Summit `gen_ai_otel/observation` (types/baseline/analyzer/scaling), generalized to the four-feature model.

Maps to: Implementation Plan S1; FR-2/FR-3; NFR-2; TC-D-*, TC-F-03/04/13.

## S2 — OTel emission + operator (Kopf)
- `otel/attributes.py`: gen_ai.agent.* / gen_ai.evaluation.* constants, verbatim from the Obs-Summit semconv. No `drift.*` namespace (C1). Additive: `gen_ai.agent.gate.action`, `computed.anomaly.kind=arg_schema_novel`.
- `otel/emit.py`: `build_span_attributes` (identity/baseline/gate on the span) + `build_evaluation_event` (score on the event); `Emitter` pushes OTLP or no-ops if OTel isn't installed (graceful).
- `operator/policy.py`: cluster-free `validate()` (TC-F-01) + `Policy`; `model_seed` surfaces an optional `models:` source (FR-9).
- `operator/reconcile.py`: `Reconciler` builds a live `BaselineStore` (memory/sqlite), folds runs, computes `status` (`baselineReady`, `observedTaskTypes`) — operator-written, never the user.
- `operator/main.py`: Kopf validate/create/update/delete handlers; imports cleanly without kopf.
- `config/otel-collector.yaml` (OTLP→Jaeger/Prometheus) + `config/otel-targets.yaml` (decoupled endpoint, host.k3d.internal:4317).
- `tests/test_operator_otel.py`: 7 tests — valid/invalid policies, model-seed, reconcile status, OTel schema conformance (TC-F-08: no drift.* keys, score on event in [0,1]). 16 total green.

Maps to: Implementation Plan S2; FR-4/5/6/9; Constraints C1; TC-F-01/02/08.

## S3 — Interceptor + runtime adapters
- `adapters/`: built-in `kagent` + `goose` (both normalize to the same DecisionChain — one policy governs both) + `custom_example` (the `custom` adapter path, FR-8). Registered on import via the SDK registry.
- `interceptor/engine.py`: transport-free `Interceptor` — normalize → score → enforce. Three actions: `log` (forward+flag), `drop` (silent 200 no-op), `block` (403 before kube-apiserver). Cold-start and exceptions fail to the declared `failurePolicy` (NFR-6). Emits the gen_ai.agent.* schema per call.
- `interceptor/server.py`: FastAPI sidecar (`/v1/tool-call`, `/healthz`) over the engine; FastAPI/uvicorn optional.
- `tests/test_interceptor_adapters.py`: 10 tests — kagent/goose same-shape, custom-by-name + builtin/ resolution, log/drop/block outcomes & status codes, happy-path forward, failClosed/failOpen resilience, cold-start failClosed. 26 total green.

Maps to: Implementation Plan S3; FR-1/7/8; NFR-1/6; TC-F-05/06/07/09/10/11.

## S4 — Demo stack (Helm + k3d + podman-compose + five scenarios)
- `cli.py`: `driftwatch demo <scenario>` runs all five scenarios through the real detection core (standalone — demo-safe, no cluster needed; identical in-cluster). Tiny SRE tool catalog (category/risk). `eval` subcommand wired to S5.
- `examples/k3d-cluster-demo/`: `k3d-config.yaml` (cluster, host.k3d.internal), `compose.yaml` (OTel Collector + Jaeger + Prometheus + Grafana + Neo4j on podman-compose), `grafana-dashboard.json` (agent-decisions: gate.action, score.value p95, anomaly kinds, FP rate), README + DEMO_RUNBOOK (35-min beat sheet) + recordings/ placeholder.
- `config/prometheus.yaml`: scrape the collector's drift metrics.
- `deploy/helm/driftwatch/`: Chart + values + values-k3d (OTLP→host.k3d.internal) + templates (operator Deployment, RBAC ClusterRole/binding, CRD install). CRD vendored into the chart.
- `make demo-1..5` / `cluster-up` / `obs-up` / `deploy` all wired.
- `tests/test_cli_demos.py`: all five scenarios pass with correct anomaly.kind + action (tool→baseline_mismatch/block, scope→scope_creep/block, sequence→blocked_transition/drop, arg→arg_schema_novel/block, storm→drop). 33 total green.
- Fix: demo interceptor shares the baseline's tool catalog so category/risk match (prevents spurious risk-escalation); sequence drift attributed to the destination tool.

Maps to: Implementation Plan S4; Benefits §1; NFR-5; TC-D-02..07 end-to-end.
