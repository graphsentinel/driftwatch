# Plan — Consensus Baseline + MCP-Proxy Enforcement (E8 + E7)

Design-only (no code yet). Two linked epics that complete the "learn normal from a model
panel, then police real Kagent against it" story:

- **E8 — Consensus baseline seed**: build a per-task baseline from a *panel* of
  open-source models, keeping only what the majority agree is normal.
- **E7 — MCP-proxy enforcement (path B)**: score a real, Helm-installed Kagent's tool
  calls against that baseline at the MCP hop (already sketched in the CFP; restated here
  with the consensus link).

These reuse what already exists and are intentionally small additions:
- `Policy.model_seed` already parses a `{models: [...]}` baseline source (FR-9).
- `Reconciler.seed_from_models(expected_chains)` already folds seed chains — it just has
  no producer today. E8 is that producer.
- `library/baseline.py` `fold()` adds *every* tool it sees; consensus must filter
  **before** folding, so a single model's hallucination never enters the baseline.

---

## E8 — Consensus baseline seed

**Goal.** For each task type, ask N open-source models "what tool calls would you make?",
collect their proposed chains, and distill a **majority** baseline: a tool (or scope, or
transition) is "normal" only if ≥ a quorum of distinct models proposed it. One-model and
minority proposals are dropped.

**Why majority (the chosen design).** It is explainable on stage ("4 of 6 models agreed
these five tools are how you investigate latency"), robust to a single model's
hallucination, and maps cleanly onto the existing `fold()` — we fold ONE synthesized
"consensus chain" per task rather than every raw chain.

**Decided constraints.**
- Consensus rule: **majority tool-set** — keep tools proposed by ≥ ⌈N/2⌉ distinct models.
  (Sequence n-grams and scopes: keep a transition/scope if ≥ quorum models produced it.)
- Execution: **offline CLI**, writes the baseline to the sqlite/JSON store the operator
  loads. The operator pod never calls an LLM (clean separation, stage-safe, no network
  dependency at reconcile time). Same `DRIFTWATCH_DATA_DIR` store the operator reads.
- Models come from `Policy.model_seed` (the CRD `baseline.sources: [{models: [...]}]`).

### Tasks
- **T8.1** — `consensus/runner.py`: an Ollama client (`OLLAMA_HOST`, default
  `localhost:11434`). For each (task, model) ask for a tool-call list; parse the reply
  into a `DecisionChain` via the existing adapters/fingerprint. Cloud-routed models
  (`*-cloud`) and local models both work through the same `/api/generate`.
- **T8.2** — `consensus/aggregate.py`: pure, cluster-free. Input: `{task -> {model ->
  [chains]}}`. Output: one synthesized **consensus `DecisionChain`** per task containing
  only majority tools/transitions/scopes. Quorum is configurable (default ⌈N/2⌉).
  This is the only new detection logic and it is unit-testable without Ollama.
- **T8.3** — `cli.py consensus-seed --policy <file> --out <dir>`: wire T8.1 → T8.2 →
  `Reconciler.seed_from_models()`; persist the seeded store. Prints a per-task panel
  table (which model proposed what, what survived quorum).
- **T8.4** — Provenance: write `consensus_seed.json` (per task: models polled, raw
  proposals, quorum, surviving tool-set) so the baseline is auditable — the same
  "results/" discipline as eval.
- **T8.5** — Honesty guard: if fewer than 2 models answer for a task, refuse to seed that
  task (log it) rather than build a baseline from one voice.

### Definition of Done
- [ ] `consensus-seed` against a panel (e.g. `qwen3.5:4b`, `gemma4:31b-cloud`, + locals)
      produces a baseline where a tool only one model proposed is **absent**, and a tool
      the majority proposed is **present**.
- [ ] `aggregate.py` has unit tests with synthetic proposals (no Ollama): quorum math,
      minority drop, tie handling, single-model refusal.
- [ ] The seeded store loads in the operator and `kubectl get adp` shows
      `baselineReady: true` with the consensus task types.
- [ ] `consensus_seed.json` records provenance for every seeded task.
- **Test Cases:** TC-F-15 (majority keep / minority drop), TC-F-16 (single-model refusal),
  TC-F-17 (seeded store → operator baselineReady).

### Gherkin
```gherkin
Feature: Consensus baseline from a model panel

  Scenario: A tool only one model proposes is excluded
    Given 4 models proposing chains for task "investigate_latency"
    And only 1 of them proposes "DeleteNamespace"
    When the consensus baseline is built with majority quorum
    Then "DeleteNamespace" is NOT in the baseline's expected tools
    And tools proposed by >= 2 models ARE in the baseline

  Scenario: Refuse to seed a task with too few voices
    Given only 1 model answered for task "rare_task"
    When consensus seeding runs
    Then "rare_task" is skipped (not seeded from a single model)
    And the skip is recorded in consensus_seed.json
```

---

## E7 — MCP-proxy enforcement against the consensus baseline (path B)

(See the CFP E7 section for the full task list; this restates the seam to E8.)

Real Kagent is Helm-installed and controller-managed; its tool calls leave the agent pod
over MCP Streamable HTTP to ToolServer pods. DriftWatch registers as an **MCP proxy** via
Kagent's `RemoteMCPServer`, scores each `tools/call` with `Interceptor.handle()` against
the **E8 consensus baseline**, and forwards survivors to the real ToolServer.

**The E8↔E7 seam:** E8 produces the baseline the E7 proxy reads. Same `BaselineStore`,
same `score_chain` — E7 adds only the MCP transport shell (already specified in the CFP:
`tools/list` passthrough, `tools/call` → ToolCall → handle → forward/MCP-error).

### Order of work
1. **E8 first** — without a trustworthy baseline there is nothing to enforce against.
2. **E7 second** — point a real Kagent at the proxy; confirm a `tools/call` outside the
   consensus baseline returns an MCP error and never reaches the ToolServer.

---

## What this is NOT (scope guard)
- Not retraining or fine-tuning models — DriftWatch only *reads* their proposed chains.
- Not capability-weighted (we keep majority, not weighted, so it doesn't contradict the
  inverse-scaling finding that bigger ≠ safer).
- Not operator-embedded LLM calls — seeding is offline; the operator stays LLM-free.
