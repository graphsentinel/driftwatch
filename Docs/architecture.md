# Architecture (implementation)

How the code maps to the CFP architecture. Control plane in the cluster; observability
decoupled in podman-compose; the score happens on the agent's decision, upstream of the
API.

```
                 Control plane (k3d)                  Data plane (per agent pod)
   ┌──────────────────────────────────┐      ┌───────────────────────────────────┐
   │ operator/  (Kopf)                 │      │ interceptor/ (FastAPI sidecar)    │
   │   policy.validate  (webhook)      │      │   adapter.normalize  -> ToolCall  │
   │   reconcile.Reconciler            │      │   library.score_chain             │
   │     -> db/ BaselineStore          │◄─────┤   apply log / drop / block        │
   │     -> status{ready,taskTypes}    │ base │   otel.emit  (gen_ai.agent.*)     │
   └──────────────────────────────────┘ line └─────────────────┬─────────────────┘
                                                                │ OTLP push
                                                                ▼  host.k3d.internal:4317
                       Observability stack (podman-compose, decoupled)
            OTel Collector ─► Jaeger (traces) / Prometheus (metrics) / Neo4j (graph)
                                          └► Grafana (agent-decisions)
```

## Module map

| Layer | Package | Role |
|---|---|---|
| contract | `sdk/` | `ToolCall`, `DecisionChain`, `RuntimeAdapter` — the stable boundary |
| detection | `library/` | fingerprint, ngram, zscore, baseline, decision, scaling — pure stats |
| persistence | `db/` | `MemoryBackend` / `SqliteBackend` behind one interface |
| forensics | `graph/` | optional Neo4j decision-graph (default-off) |
| emission | `otel/` | `gen_ai.agent.*` span attrs + `gen_ai.evaluation.result` event |
| control plane | `operator/` | Kopf reconcile + validate; cluster-free `policy`/`reconcile` |
| data plane | `interceptor/` | enforcement engine + FastAPI sidecar |
| adapters | `adapters/` | built-in kagent/goose + custom example |
| eval | `evaluation_runner.py` | recall / FP / p95 / inverse-scaling |
| demo | `cli.py`, `examples/` | five scenarios, k3d + compose |

## Key invariant

The score is computed on the **decision chain** (which tool, which scope, which order,
which arg shape) — not on the resulting API object. By the time admission would see a
`CREATE`, DriftWatch has already decided whether the agent *should have asked*. In
`log` mode it records that judgment without enforcing; that is the trust-building path
before flipping to `block`.

## OTel compliance (Constraints C1)

Only the `gen_ai.agent.*` schema is emitted — no `drift.*` namespace. The score lives
on the `gen_ai.evaluation.result` event; identity/baseline/gate/anomaly live on the
span. DriftWatch's only additive contributions: `gen_ai.agent.gate.action` and the
`computed.anomaly.kind` value `arg_schema_novel`.
