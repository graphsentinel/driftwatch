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
