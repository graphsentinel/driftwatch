# E13 — Multi-app governance: one central DriftWatch, N AgentGates

**Status:** implemented. **Question it answers:** *"birden fazla AgentGate bir DriftWatch'a nasıl
bağlanır?"* — connect many AgentGate apps to a single DriftWatch governance plane, each governed
against its **own** declared contract, without the apps overwriting each other.

> **Routing contract (one line):** *If a tool call carries `_meta.app`, it must match a registered
> contract — otherwise DriftWatch blocks it as `unknown_app`. A call with no `_meta.app` uses the
> default/single contract (legacy/sidecar), unchanged.*

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
   - **STRICT** when a populated registry is in play: `_meta.app` selects that app's contract; an
     `app_ref` that names **no** registered contract is an *unknown app* → blocked (`unknown_app`
     declared violation), **never** silently governed by another app's contract. Only a call with **no**
     `_meta.app` (legacy / single-app / sidecar) falls back to the default contract.
   - `_meta.agent or chain.agent_id` → the agent identity for the declared check (so one shared proxy
     seat governs every agent of every app; the env-set `agent_id` is only the fallback).

   `baseline` stays keyed by `task_type` (shared/shareable); **chains** stay isolated per MCP session.

   *Consequence of strict mode:* if an app's contract push hasn't landed yet (DriftWatch was down at
   the app's startup), that app's calls block as `unknown_app` until it re-pushes — a loud, safe
   misconfiguration signal rather than silent ungoverned forwarding. (`proxyType=driftwatch` means the
   user *expects* governance.)

### Ref safety

The push `ref` becomes a filename (`<ref>.json`), so it is whitelist-validated — `valid_ref` /
`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`: must start alphanumeric, then alphanumerics + `_.-`, ≤64 chars.
This rejects `/`, `\`, `..` (leading dot), spaces, and every path separator, closing a path-traversal
hole independent of auth. `/contracts` returns **400** on a bad ref; `save_contract` raises and
`load_contract`/`delete_contract` treat an unsafe name as absent (defense-in-depth).

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

- **AuthZ on `/contracts`** — any *authenticated* caller can push under any (valid) `ref`. Input is
  now validated (ref whitelist above), but there is no caller authentication/authorization yet:
  multi-app correctness (this doc) is independent of access control; auth is tracked separately
  (consultant #2 / deferred).
- **Empty-tools agent = unconstrained** — `proxyType=driftwatch` auto-binds the proxy to every agent;
  an agent with no declared `tools` is unconstrained for the per-call bind check (degrades cleanly).
  Fine for demos; production should require explicit `tools` or `allow`/`deny` (consultant #3).

## Code

- `interceptor/engine.py` — `Interceptor.contracts`, strict `_meta` routing in `handle` (unknown_app).
- `library/contract.py` — `load_all_contracts(data_dir)`, `valid_ref` + hardened save/load/delete.
- `interceptor/server.py` — `apply_contract_push` (transport-free; ref store, no overwrite).
- `interceptor/mcp_proxy.py` — `_add_contracts_route`, factory shares `contracts`, `run()` seeds it.
- AgentGate `server.py` `_maybe_govern` (push `ref`) + `codegen/runtime.py` (`_meta.app`).

## Tests

- `tests/test_interceptor_adapters.py` — `_meta.app` routing, unknown-app fallback, `_meta.agent`
  override, `apply_contract_push` no-overwrite, `load_all_contracts`.
- `tests/test_mcp_proxy.py` — proxy mounts `/contracts`, push seen by session interceptors + routes.
- AgentGate `tests/test_codegen.py` — `_meta.app` stamping, push `ref` from env, `govern.app` export.
