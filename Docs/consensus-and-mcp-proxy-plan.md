# Plan — Consensus Baseline (FR-9) + MCP-Proxy Enforcement (E7)

Design-only (no code yet). This is **not new scope** — both pieces are already in the CFP;
this doc is the implementation plan for the parts that are specified but not yet coded:

- **Consensus baseline seed = FR-9**, already in the CFP: the CRD example shows
  `sources: [..., {models: [qwen, gemma]}]` ("2+ models vote on the expected chain to seed
  a baseline"), FR-9 defines it, and **TC-F-15** ("Model seed + handover") is its test.
  What's missing is only the *producer* that turns those models' votes into the seed.
- **MCP-proxy enforcement = E7** (CFP Epic E7, TC-F-16/17): score a real, Helm-installed
  Kagent's tool calls against that baseline at the MCP hop.

Together they complete the "learn normal from a model panel, then police real Kagent
against it" story — using scaffolding that already exists in the tree.

What already exists (verified):
- `Policy.model_seed` parses the `{models: [...]}` baseline source (FR-9 wiring).
- `Reconciler.seed_from_models(expected_chains)` folds seed chains — but has **no
  producer** today. The consensus builder below is that producer.
- `library/baseline.py` `fold()` adds *every* tool it sees, so consensus must filter
  **before** folding, or a single model's hallucination enters the baseline.

---

## Part 1 — Consensus seed producer (implements FR-9)

**Goal.** For each task type, ask N open-source models "what tool calls would you make?",
collect their proposed chains, and distill a **majority** baseline: a tool (or scope, or
transition) is "normal" only if a quorum of distinct models proposed it. Minority and
single-model proposals are dropped, then the surviving consensus chain is folded via the
existing `Reconciler.seed_from_models()`.

**Chosen design (your decisions).**
- Consensus rule: **majority tool-set** — keep tools proposed by ≥ ⌈N/2⌉ distinct models
  (same quorum for scopes and sequence transitions). Not capability-weighted — majority,
  so it does NOT contradict the inverse-scaling finding (bigger ≠ safer).
- Execution: **offline CLI**, writes the baseline to the sqlite/JSON store the operator
  loads (`DRIFTWATCH_DATA_DIR`). The operator pod never calls an LLM — stage-safe, no
  network dependency at reconcile time.
- Models come from `Policy.model_seed` (the CRD `baseline.sources: [{models: [...]}]`).

### Tasks
- **T-C1** — `consensus/runner.py`: Ollama client (`OLLAMA_HOST`, default
  `localhost:11434`). For each (task, model) request a tool-call list; parse into a
  `DecisionChain` via the existing adapter/fingerprint. Local and `*-cloud` models both
  go through `/api/generate`.
- **T-C2** — `consensus/aggregate.py`: pure, cluster-free, Ollama-free. Input
  `{task -> {model -> [chains]}}` → one synthesized **consensus `DecisionChain`** per task
  with only majority tools/transitions/scopes. The only new detection logic; fully
  unit-testable. Quorum configurable (default ⌈N/2⌉).
- **T-C3** — `cli.py consensus-seed --policy <file> --out <dir>`: wire T-C1 → T-C2 →
  `Reconciler.seed_from_models()`; persist the seeded store; print a per-task panel table.
- **T-C4** — Provenance: `consensus_seed.json` (per task: models polled, raw proposals,
  quorum, surviving tool-set) — same "results/" audit discipline as eval.
- **T-C5** — Honesty guard: if < 2 models answer for a task, refuse to seed it (log) rather
  than build a baseline from one voice.

### Definition of Done
- [ ] `consensus-seed` against a panel (e.g. `qwen3.5:4b`, `gemma4:31b-cloud`, + locals)
      yields a baseline where a one-model-only tool is **absent** and a majority tool is
      **present**.
- [ ] `aggregate.py` unit-tested with synthetic proposals (no Ollama): quorum math,
      minority drop, tie handling, single-model refusal.
- [ ] The seeded store loads in the operator; `kubectl get adp` shows `baselineReady:true`
      with the consensus task types — this is exactly **TC-F-15** end-to-end.
- [ ] `consensus_seed.json` records provenance for every seeded task.
- **Test Cases:** **TC-F-15** (model seed + handover — the existing CFP case, now
  executable); **TC-F-18** (majority keep / minority drop); **TC-F-19** (single-model
  refusal). *(TC-F-18/19 were added to the CFP test catalog for this; E7 owns TC-F-16/17.)*

### Gherkin
```gherkin
Feature: Consensus baseline from a model panel (FR-9)

  Scenario: A tool only one model proposes is excluded          # TC-F-18
    Given 4 models proposing chains for task "investigate_latency"
    And only 1 of them proposes "DeleteNamespace"
    When the consensus baseline is built with majority quorum
    Then "DeleteNamespace" is NOT in the baseline's expected tools
    And tools proposed by >= 2 models ARE in the baseline

  Scenario: Refuse to seed a task with too few voices           # TC-F-19
    Given only 1 model answered for task "rare_task"
    When consensus seeding runs
    Then "rare_task" is skipped (not seeded from a single model)
    And the skip is recorded in consensus_seed.json

  Scenario: Seed then hand over to real runs                    # TC-F-15 (existing)
    Given a baseline seeded from the model panel
    When real successful runs accumulate in the window
    Then real runs progressively replace the seed
```

---

## Part 2 — E7 MCP-proxy enforcement against the consensus baseline (path B)

(Full task list lives in the CFP E7 section; this only restates the seam to FR-9.)

Real Kagent is Helm-installed and controller-managed; its tool calls leave the agent pod
over MCP Streamable HTTP to ToolServer pods. DriftWatch registers as an **MCP proxy** via
Kagent's `RemoteMCPServer`, scores each `tools/call` with `Interceptor.handle()` against
the **FR-9 consensus baseline**, and forwards survivors to the real ToolServer.

**The seam:** Part 1 produces the baseline the E7 proxy reads. Same `BaselineStore`, same
`score_chain` — E7 adds only the MCP transport shell (CFP: `tools/list` passthrough,
`tools/call` → ToolCall → handle → forward / MCP-error). Tests **TC-F-16/17** (in the
CFP E7 section + test catalog).

### Order of work
1. **Part 1 (FR-9 producer) first** — no trustworthy baseline → nothing to enforce.
2. **E7 second** — point a real Kagent at the proxy; a `tools/call` outside the consensus
   baseline returns an MCP error and never reaches the ToolServer.

---

## Scope guard (what this is NOT)
- Not new CFP scope — FR-9 and E7 were always in the proposal; this implements them.
- Not retraining/fine-tuning — DriftWatch only *reads* models' proposed chains.
- Not capability-weighted — majority, so it doesn't contradict the inverse-scaling finding.
- Not operator-embedded LLM calls — seeding is offline; the operator stays LLM-free.
