# E13 — Multi-app governance: one central DriftWatch, N AgentGates

**Status:** implemented. **Question it answers:** *"birden fazla AgentGate bir DriftWatch'a nasıl
bağlanır?"* — connect many AgentGate apps to a single DriftWatch governance plane, each governed
against its **own** declared contract, without the apps overwriting each other.

## The problem

Before this change DriftWatch held a **single** declared contract:

- `interceptor.contract` was one field; the `/contracts` push did `interceptor.contract = c`
  (last-push-wins) and persisted under a `ref` that defaulted to `"agentgate"`.
- So two AgentGates pushing to one DriftWatch would **overwrite** each other — app B's agents would be
  judged against app A's contract.

Per-session tool-call **chains** were already isolated (the MCP proxy keys interceptors on
`fastmcp_context.session_id`). Only the **declared contract** was shared/single.

## The design — registry + routing

Three pieces, all back-compatible (no `_meta` / single-app deploy behaves exactly as before):

1. **Per-app `ref`.** Each AgentGate pushes its contract under its own app id (`AGENTGATE_APP` env,
   set by the chart from `.Values.app`, default release name; `govern.app`/`govern.ref` in the CR
   overrides and is exported to env so it stays the single source). The push body carries `ref`.

2. **Contract registry.** `Interceptor.contracts: dict[str, DeclaredContract]` (`{app_ref ->
   contract}`) replaces the single field as the store. `/contracts` writes `contracts[ref] = c`
   (no cross-app overwrite); the first push also seeds the metaless default `self.contract`.
   Seeded at startup from every persisted contract via `load_all_contracts(data_dir)` so a restart
   recovers all apps. Persisted as `<DATA_DIR>/contracts/<ref>.json` (one file per app).

3. **Tool-call routing.** Every AgentGate tool call carries `_meta.app` (the same app id) and
   `_meta.agent` (the calling agent). `Interceptor.handle` routes:
   - `_contract_for(_meta.app)` → that app's contract (falls back to the default when there is no
     registry / no ref / unknown ref);
   - `_meta.agent or chain.agent_id` → the agent identity for the declared check (so one shared proxy
     seat governs every agent of every app; the env-set `agent_id` is only the fallback).

   `baseline` stays keyed by `task_type` (shared/shareable); **chains** stay isolated per MCP session.

## Topology

The MCP proxy (`driftwatch-mcp`) is **both** the tool path and the contract receiver: it mounts
`POST /contracts` (+ `GET /healthz`) as a FastMCP `custom_route`, owns one in-memory registry, and
seeds it from disk at startup. Per-session interceptors share that registry **by reference**, so a
push is seen live by every session (the registry is mutated in place, never rebound).

```
AgentGate-1 (app=checkout)  ──push contract (ref=checkout)──┐
AgentGate-2 (app=billing)   ──push contract (ref=billing)───┤
AgentGate-3 (app=ops)       ──push contract (ref=ops)───────┤
                                                            ▼
                                          ┌──────────── DriftWatch (central) ───────────┐
   tool call _meta.app=checkout ─────────▶│ POST /contracts → registry{checkout,billing,│
   tool call _meta.app=billing  ─────────▶│   ops}                                       │
   tool call _meta.app=ops      ─────────▶│ on_call_tool → _contract_for(_meta.app) →    │
                                          │   declared check vs the RIGHT contract → fwd │
                                          └──────────────────────────────────────────────┘
```

### Persistence

In-memory registration always works. To survive a **proxy restart** without every AgentGate
re-pushing, set `mcpProxy.persistContracts=true` — it mounts the shared PVC writable so pushed
contracts (`/data/contracts/*.json`) persist and reload via `load_all_contracts`. Default is
read-only (the operator-writes / proxy-reads baseline invariant is kept; apps re-push on restart).

## What did NOT change

- **Chain isolation** — already per `session_id`; unchanged.
- **Statistical baseline** — per `task_type`; shared across apps by design (a task's normal shape is
  app-independent). Scope it per app via distinct `task_type`s if needed.
- **Standalone / single-app** — no `_meta.app`, no registry → falls back to the single contract.
  Every pre-existing test stays green.

## Out of scope (deferred)

- **AuthZ on `/contracts`** — any caller can currently push under any `ref`. Multi-app correctness
  (this doc) is independent of access control; auth is tracked separately (consultant #2 / deferred).

## Code

- `interceptor/engine.py` — `Interceptor.contracts`, `_contract_for`, `_meta` routing in `handle`.
- `library/contract.py` — `load_all_contracts(data_dir)`.
- `interceptor/server.py` — `apply_contract_push` (transport-free; ref store, no overwrite).
- `interceptor/mcp_proxy.py` — `_add_contracts_route`, factory shares `contracts`, `run()` seeds it.
- AgentGate `server.py` `_maybe_govern` (push `ref`) + `codegen/runtime.py` (`_meta.app`).

## Tests

- `tests/test_interceptor_adapters.py` — `_meta.app` routing, unknown-app fallback, `_meta.agent`
  override, `apply_contract_push` no-overwrite, `load_all_contracts`.
- `tests/test_mcp_proxy.py` — proxy mounts `/contracts`, push seen by session interceptors + routes.
- AgentGate `tests/test_codegen.py` — `_meta.app` stamping, push `ref` from env, `govern.app` export.
