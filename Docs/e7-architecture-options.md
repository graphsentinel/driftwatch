# E7 architecture — two options for the real-Kagent enforcement hop

**Design only, no code.** Before implementing E7 we compare two ways to put DriftWatch on
the live MCP tool-call path for real, Helm-installed Kagent. This supersedes the single
approach assumed in `e7-mcp-proxy-design.md` (Option A below) by adding the gateway/ext_authz
approach (Option B) and a recommendation.

Both reuse the detection core unchanged: `Interceptor.handle(dict) -> Verdict`, scoring a
decision chain against the operator-reconciled baseline (FR-10 shared store). The question
is only **who owns the MCP transport** and **how DriftWatch is invoked**.

---

## Option A — DriftWatch is its own MCP proxy

```
Kagent agent pod ──MCP──> DriftWatch MCP proxy ──MCP──> real MCP ToolServer
                          (speaks JSON-RPC, scores tools/call,
                           forwards survivors, blocks drift)
```

DriftWatch registers as Kagent's `RemoteMCPServer`. It must itself speak MCP: parse
JSON-RPC, pass through `tools/list`, score `tools/call`, forward survivors to the upstream
ToolServer, return an MCP error on block.

**What we'd build:** `interceptor/mcp.py` (pure JSON-RPC↔engine mapping) + `mcp_proxy.py`
(transport server, upstream forwarding) + MCP e2e. (This is exactly what was drafted/paused.)

**Pros**
- No dependency on agentgateway — DriftWatch is self-contained.
- Full control of the wire behavior.

**Cons**
- **We re-implement MCP transport** — JSON-RPC framing, `tools/list` passthrough, upstream
  forwarding, and (for real Kagent) MCP Streamable-HTTP: SSE streaming + session lifecycle.
  That's the fragile, easy-to-get-subtly-wrong surface, and it's not DriftWatch's value.
- DriftWatch has to masquerade as a ToolServer/proxy and stay current with the MCP spec.
- Duplicates what a purpose-built agentic proxy already does.

---

## Option B — agentgateway in front, DriftWatch as external authorization (your proposal)

```
Kagent agent pod ──MCP──> agentgateway ──MCP──> real MCP ToolServer
                              │
                              └── ext_authz (per tools/call) ──> DriftWatch
                                  HTTP 200 = allow, non-2xx = deny
```

agentgateway is Kagent's own AI-native proxy (MCP/A2A) and supports **External
Authorization**: for each request it calls an external service and **allows on HTTP 2xx,
denies otherwise** (it is API-compatible with the Envoy ext_authz model, and has an
MCP-aware authz path that evaluates `call_tools` invocations).

**Key fit:** DriftWatch's *existing* `/v1/tool-call` endpoint already returns **200 for
forward and 403 for block** — i.e. it is already an ext_authz-shaped allow/deny service.
agentgateway owns all MCP transport; DriftWatch only answers "allow this tool call?".

**What we'd build:** almost nothing new in DriftWatch — adapt/confirm the `/v1/tool-call`
contract matches what agentgateway's HTTP ext_authz sends (tool name + arguments in the
body), plus deploy wiring (an agentgateway config that points ext_authz at the DriftWatch
Service) + an example. The drift logic, baseline, FR-10 handoff are all reused as-is.

**Pros**
- **Minimal new code** — no MCP transport in DriftWatch; the mature proxy handles framing,
  streaming, sessions, `tools/list`.
- agentgateway is the *native, supported* place to govern Kagent tool calls — architecturally
  honest ("we plug into the agent gateway as a drift authorizer"), not a bolt-on proxy.
- Consistent with the earlier "don't open a separate fragile surface" calls (e.g. deferring
  the admission webhook).
- Reuses the already-tested 200/403 endpoint → most of E7 becomes config + an e2e.

**Cons**
- Adds a runtime dependency on agentgateway (must be installed in the path-B cluster).
- The **chain-state nuance** (below) — ext_authz is invoked per single tool call, but
  DriftWatch scores ordered *chains*.

---

## The chain-state nuance (applies to both, sharper in B)

DriftWatch scores a **decision chain** (ordered sequence of tool calls per task), not just
one isolated tool. In a per-call authorization hop, each call arrives separately, so
DriftWatch must accumulate the chain across calls keyed by something stable (session /
agent_id / task). The current `Interceptor` already appends each observed call to a chain on
its adapter — so single-process accumulation exists; what E7 needs is to **key chains by the
caller** so two concurrent agents don't share one chain.

- **Option A:** DriftWatch sees the whole MCP session, so it can hold per-session chain
  state itself.
- **Option B:** agentgateway must pass a stable correlation id (session/agent) in the
  ext_authz request so DriftWatch can bucket calls into the right chain. Need to confirm
  agentgateway forwards an MCP session id / identity to the ext_authz call; if not, fall
  back to per-(agent,task) keying from the request, or score per-call features only (tool,
  scope, argSchema) at the hop and keep sequence scoring for the trace/postmortem.

This is the one real design item to resolve for B and must be verified against agentgateway's
ext_authz request contract before implementing.

---

## Recommendation

**Pursue Option B (agentgateway + DriftWatch ext_authz) as the primary path**, keep Option A
documented as the fallback. Rationale: B keeps DriftWatch focused on its actual value (drift
detection), reuses the already-tested 200/403 contract, and lets a mature, Kagent-native
proxy own the transport — far less fragile than re-implementing MCP. The cost is one
dependency + resolving chain correlation.

**Sequencing (revised):**
1. Confirm agentgateway's HTTP ext_authz request contract — does the body carry the MCP
   tool name + arguments, and is there a session/identity field for chain correlation?
   (doc/spec check; no cluster needed.)
2. Make `/v1/tool-call` accept whatever shape agentgateway sends (a thin request-adapter if
   needed) — pure, unit-testable, no agentgateway required.
3. Chain correlation: key chains by the forwarded session/agent id (or document the per-call
   fallback). Unit-tested.
4. Deploy wiring: an agentgateway config + DriftWatch Service example, and an e2e — gated on
   agentgateway being installed (you indicated it isn't yet, so steps 1–3 are the work now;
   step 4 is later).

Until agentgateway is available, the in-process `make demo` + path-A stand-in remain the
deterministic demo, exactly as today.

---

## What to keep from the paused Option-A draft

The pure mapping idea (MCP JSON-RPC ↔ engine dict) is reusable in B too if agentgateway is
configured to forward the raw JSON-RPC body to ext_authz. So step 2 above can borrow the
`to_engine_call` mapping from the Option-A draft regardless of which option ships.
