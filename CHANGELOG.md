# Changelog

Sprint-by-sprint delivery history (see the CFP's Implementation Plan, S0–S6).
Each entry corresponds to one sprint commit.

## S0 — Scaffold
- Repo skeleton matching the CFP Repository Layout (`src/driftwatch/{library,sdk,adapters,db,graph,otel,operator,interceptor,crd}`, `config/`, `deploy/`, `evaluation/`, `examples/k3d-cluster-demo/`, `tests/`).
- `pyproject.toml` (package `driftwatch` v0.1.0a0, optional extras: operator/interceptor/graph/dev).
- `AgentDriftPolicy` CRD manifest (`deploy/crd/`) with OpenAPI v3 validation; `status` is a subresource (operator-written).
- Sample policies: `kagent-cluster-ops` (enforce) + `shadow-mode` (log) under `config/policies/`.
- `Makefile` (install/test/eval/cluster-up/obs-up/demo-1..5), `.gitignore`, CI workflow, README.
