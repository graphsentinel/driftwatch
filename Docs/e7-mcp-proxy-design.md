# E7 — MCP-proxy enforcement design (path B, real Kagent)

**Status: design only. Implementation is sequenced LAST** (after FR-9 `runner.py` and the
webhook sidecar injector). This doc is the plan so the implementation, when it lands, is
mechanical and grounded in what already exists.

## Why E7 is small in code, large in integration

The detection core is already transport-agnostic. `Interceptor.handle(raw_call: dict)` takes
a plain dict, normalizes via the runtime adapter, scores against the baseline, and returns a
`Verdict(outcome, http_status, decision, signals)`. `KagentAdapter.normalize` already maps
the MCP-ish shape `{"tool", "namespace", "args"}`. So E7 adds **no new detection logic** —
it adds a **transport shell** that speaks MCP on the wire and calls the existing engine.

The weight of E7 is *integration*, not algorithm: a real Helm-installed Kagent, the MCP
Streamable-HTTP protocol, a ToolServer to forward survivors to, and a model provider
(OpenAI/etc.) for Kagent itself. That is why it is sequenced last and gated on a real
cluster + credentials being available.

## The integration point (verified understanding)

Real Kagent is **Helm-installed and controller-managed**: an `Agent` CRD → a controller-
created agent pod. Tool calls do **not** stay in-pod — they leave the agent pod over **MCP
Streamable HTTP** to separate **MCP ToolServer** pods. So DriftWatch's enforcement seam is
the **MCP tool-call hop**, not a sidecar on localhost (that's path A). DriftWatch registers
as an **MCP proxy** via Kagent's `RemoteMCPServer`: Kagent points at DriftWatch, DriftWatch
scores each `tools/call` and forwards survivors to the real ToolServer.

```
Kagent agent pod ──MCP──> DriftWatch MCP proxy ──MCP──> real MCP ToolServer
                          (score tools/call;            (executes the tool)
                           block => MCP error)
```

This reuses the FR-9 consensus baseline (or any reconciled baseline) and `score_chain`
unchanged — same `BaselineStore`, same shared store the operator writes (FR-10 seam).

## What to build

### T-E7.1 — MCP proxy server (`interceptor/mcp_proxy.py`)

A second ASGI app (sibling to `server.py`), speaking MCP Streamable HTTP. Two methods:

- **`tools/list`** — passthrough: forward to the upstream ToolServer, return its tool list
  unchanged. DriftWatch does not invent or hide tools; it only governs calls.
- **`tools/call`** — the enforcement hop:
  1. map the MCP `tools/call` params → the same dict `KagentAdapter` already eats
     (`{"tool": name, "namespace": <from args/scope>, "args": arguments}`),
  2. `verdict = interceptor.handle(that_dict)` (existing engine, existing baseline),
  3. `forward`/`drop` → proxy the call to the upstream ToolServer, return its result;
     `block` → return an **MCP error** (JSON-RPC error object) and never touch the upstream.

The upstream ToolServer URL comes from config (env/values), like `DRIFTWATCH_UPSTREAM_MCP`.
Reuse `build_default_interceptor()` for the engine so policy/baseline come from the same
env+shared-store path as the sidecar (FR-10) — the proxy is just a different front door.

### T-E7.2 — MCP request/response mapping (pure, unit-testable)

A small pure module that converts between MCP JSON-RPC `tools/call` and the engine's dict,
and builds the MCP success/error envelopes. This is the only fiddly bit and must be unit-
tested with **canned MCP payloads, no network** (mirrors how `aggregate.py` is pure):
- `tools/call` params → engine dict (tool name, scope/namespace extraction, args)
- `Verdict` → MCP result (forward: upstream result; block: JSON-RPC error with a clear
  `code`/`message`, e.g. drift reason + score, so the agent sees *why* it was blocked).

### T-E7.3 — Deploy wiring (Helm + Kagent `RemoteMCPServer`)

- Helm: an optional `mcpProxy.enabled` Deployment+Service running the proxy entrypoint,
  mounting the same baseline store (read-only, like the sidecar) + the policy env.
- A `RemoteMCPServer` manifest (example, under `examples/`) pointing real Kagent at the
  proxy Service. Document the real-Kagent install (the `helm install kagent ...` already in
  `examples/k3d-cluster-demo/README.md` path B) + the model-provider secret it needs.

### T-E7.4 — e2e (needs a real cluster + Kagent + model key)

`examples/.../e7-mcp-proxy-e2e.sh`, structured like `fr10-e2e.sh`:
- operator writes a baseline to the shared PVC (consensus-seed or trusted fold),
- the MCP proxy loads it (read-only) and registers as Kagent's `RemoteMCPServer`,
- drive Kagent with a within-baseline task → `tools/call` forwarded, tool executes,
- drive a drift task → `tools/call` returns an MCP error, the ToolServer is never reached.

## Tests

- **TC-F-16** — MCP `tools/call` within baseline is forwarded to the upstream ToolServer
  and its result returned (unit: mapping + a stub upstream; e2e: real Kagent).
- **TC-F-17** — MCP `tools/call` that drifts returns an MCP error and the upstream is never
  called (unit: assert no upstream call + error envelope; e2e: real Kagent).
- Mapping unit tests (T-E7.2): canned MCP payloads → engine dict → MCP envelopes, no net.

## Gherkin

```gherkin
Feature: MCP-proxy enforcement against the reconciled baseline (E7, path B)

  Scenario: A within-baseline tools/call is forwarded               # TC-F-16
    Given a baseline the operator reconciled for task "investigate_latency"
    And real Kagent pointed at DriftWatch as a RemoteMCPServer
    When Kagent issues a tools/call within that baseline
    Then DriftWatch forwards it to the upstream MCP ToolServer and returns the result

  Scenario: A drifting tools/call is blocked at the MCP hop          # TC-F-17
    Given the same reconciled baseline
    When Kagent issues a tools/call that drifts (unexpected tool/scope/sequence)
    Then DriftWatch returns an MCP error and never calls the upstream ToolServer
```

## Boundaries (what E7 is NOT)

- Not new detection logic — same `Interceptor.handle` + `score_chain` + baseline.
- Not a replacement for path A — the in-process `make demo` + stand-in sidecar stay the
  deterministic, dependency-free demo/fallback. E7 is the *real-Kagent* path.
- Not operator-embedded — the proxy is a data-plane workload like the sidecar; the operator
  still only reconciles + writes the baseline.

## Sequencing reminder

Build order across the remaining roadmap: **FR-9 `runner.py`** (live model panel for
consensus) → **webhook sidecar injector** → **E7 (this doc)**. E7 is last because it needs
the most external scaffolding (real Kagent, a model provider, an upstream ToolServer) and
benefits from consensus + injection already being in place.
