# E10 — reconnect-on-session-terminated at the call hop (design)

**Status:** design (pre-implementation). Closes the last E10 gap: a *clean, repeatable*
cross-server **call-path** e2e. Aggregation (FR-16) + cross-server scoring (FR-17) are already
validated in-cluster; only the forward-to-upstream call flakes.

## Problem

In-cluster, against real `kubernetes-mcp-server` upstreams, a *delayed* `tools/call` fails with
`McpError: Session terminated` and never reaches the upstream. Observed:

- `tools/list` (at startup, immediate) is stable — the lifespan long-lived clients fixed that.
- `tools/call` (delayed, after the proxy has been idle) is not — the upstream has meanwhile
  **terminated the idle Streamable-HTTP session** server-side; the next request reuses a
  server-closed session.
- This is **upstream session-lifecycle behavior**, reproducible even single-upstream
  (sequential `Client(url)` opens: attempts 1–2 fail `Session terminated`, attempt 3 OK).
  The upstream also 404s on `DELETE /mcp` (session teardown), confirming non-standard session
  handling.

So the proxy needs to **survive an upstream that silently drops idle sessions**: detect the
session-class failure on forward, re-establish the upstream session, and retry the forward —
**without** re-scoring, and **without** unsafe double-execution.

## Binding decisions

### D1 — Retry only the session class, nothing else
Retry **only** when the forward fails with a session-lifecycle error
(`McpError("Session terminated")`, session-not-found, or a `DELETE /mcp` 404 surfacing as a
closed session). **Never** retry on:
- a DriftWatch verdict (`block`/`drop` → `ToolError`) — that is a decision, not a transport fault;
- a genuine upstream `ToolError` (bad args, tool failure) — retrying won't help and may double-act.

Rationale: retry is a *transport-resilience* mechanism, not an enforcement or correctness one.

### D2 — Score once; retry forwards only
DriftWatch scoring (append to the `DecisionChain`, compute the decision, emit OTel) happens
**exactly once per `tools/call`**, *before* the forward. A session-class failure on forward is a
transport event *after* the governance decision was already made (`forward`). Retry re-runs **only
the upstream forward**, never the scoring. So:
- the chain is appended once (no phantom self-transition from a retried call — the bug we already
  saw when a naive client-side retry double-appended);
- the OTel decision span is single; the retry is annotated on it (D4), not a second decision.

### D3 — Idempotency / destructive-call policy
A session-class failure *usually* means the upstream never executed the tool (the session was
closed before/at request dispatch). But "usually" is not "always", so:
- **Read-class tools** (risk tier low; `get`/`list`/`watch`-shaped): retry is safe → **on**.
- **Write/destructive tools** (risk tier high; `delete`/`create`/`apply`/`scale`-shaped): retry is
  **off by default** — a second execution could double-act (delete twice, etc.). A destructive
  call that hits a session fault returns the error to the agent (which, per E9, does not retry-storm).
- The risk tier is the existing `ToolCall.risk` / `category` the detector already computes; no new
  signal. Config knob `mcpProxy.reconnect.retryDestructive` (default `false`) can override for
  upstreams known to be idempotent.

This keeps the governance project's safety posture: **we never turn a transport retry into an
unintended second destructive action.**

### D4 — Observable retries
Emit the retry on the *same* decision span (score-once), so the audit trail shows one governed
decision plus its transport recovery:
- `gen_ai.agent.gate.forward.retried = true`
- `gen_ai.agent.gate.forward.retry_count = <n>`
- `gen_ai.agent.gate.forward.retry_reason = "session_terminated"`
No new top-level event; no `drift.*` namespace (C1).

### D5 — Bounded
At most `mcpProxy.reconnect.maxRetries` (default `2`) re-establish+retry attempts, with a short
backoff. If still failing, surface the original session error to the caller (fail toward the
declared `failurePolicy`).

## Architecture

The forward path today: `on_call_tool` → (score) → `call_next` → FastMCP mount → upstream
long-lived client (from the lifespan). The flake is inside `call_next` → that client's session.

Two implementation options — to be settled by a venv probe (next step):

- **Option A — reconnect the lifespan client.** Share the per-upstream client/proxy handles with
  the middleware; on a session-class failure, close+reopen that upstream's `Client`, re-mount its
  proxy, and retry `call_next`. Keeps one routing path (mount). Cost: runtime remount semantics in
  FastMCP need verifying.
- **Option B — fresh-client call routing.** Keep the lifespan clients for `tools/list` only; in
  the middleware, on `forward`, resolve `<server>_<tool>` → (upstream, tool) and call the upstream
  with a **fresh** `Client(url)` per call (open→call→close), retrying on the session class. Bypasses
  the flaky reused session entirely; simpler reconnect, at the cost of a connection per call
  (acceptable — these are governance hops, not a hot loop; latency budget is per-call scoring, not
  connection setup). Routing/namespacing we already own (`_validate_server_names`).

**Leaning B**: it sidesteps the reused-session class of bug rather than fighting FastMCP's mount
session reuse, and reconnect becomes "open a new client and try again" — trivially correct and
score-once-friendly. Confirm with a probe that a fresh per-call `Client(url)` to
`kubernetes-mcp-server` is stable under D1/D3 before wiring it in.

## Acceptance (what "done" adds to E10)
- [ ] Clean, repeatable in-cluster within-baseline **cross-server forward** to two real upstreams,
      returning data (the gap today).
- [ ] A session-class fault is transparently recovered (read-class), the call succeeds, OTel shows
      `forward.retried=true`, scoring ran once.
- [ ] A destructive call under a session fault is **not** double-executed (retry off by default).
- [ ] Single-upstream path (E7/E8/E9) unchanged and green.
- [ ] Unit tests: session-class retry (read) succeeds; non-session error not retried; destructive
      not retried by default; score-once (chain appended once).

## Out of scope
- Fixing `kubernetes-mcp-server` session lifecycle (upstream). We make the *proxy* resilient.
- A general MCP gateway / connection pool. This is targeted resilience for session-dropping upstreams.
