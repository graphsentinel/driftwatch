"""Multi-granularity consensus aggregation (FR-9, R9 / TC-F-28).

Pure, cluster-free, LLM-free — the only new detection logic in the consensus path, so it
is fully unit-testable with synthetic proposals.

Input: per task, the chains each model proposed:
    {task_type: {model_id: [DecisionChain, ...]}}
Output: per task, a `ConsensusResult` whose `chains` are the consensus seed to fold via
`Reconciler.seed_from_models()`.

Why multi-granularity (the R9 finding). A *majority tool-set alone* is too coarse: every
tool can be individually proposed by a majority while the **combined chain/order** was
proposed by no model. Admitting the union of majority tools would then fold a synthetic
chain nobody actually proposed (TC-F-28). So quorum is applied at EACH level and the
consensus seed is built from quorum **chain-templates** (with a quorum-transition
fallback) — never from the bare union of majority tools. A transition or chain only
survives if a quorum of distinct models actually produced *that ordering*.

Quorum default: `max(2, ceil(N/2))` over the models that answered for the task. The floor
of 2 is deliberate — a single voice must never define "normal" (consistent with the NFR-8
baseline-poisoning guard and the TC-F-19 single-model refusal); the `ceil(N/2)` term
scales the bar with the panel size.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..sdk.observation import DecisionChain, ToolCall


def quorum_for(n_models: int) -> int:
    """Default quorum for a panel of `n_models`: max(2, ceil(n/2))."""
    return max(2, math.ceil(n_models / 2))


@dataclass
class ConsensusResult:
    """The consensus outcome for one task type (also the provenance record)."""

    task_type: str
    seeded: bool
    n_models: int
    quorum: int
    chains: list[DecisionChain] = field(default_factory=list)  # consensus seed to fold
    reason: str = ""                                           # why skipped, if not seeded
    provenance: dict = field(default_factory=dict)             # per-level vote counts

    def to_json(self) -> dict:
        """Audit record for consensus_seed.json (no DecisionChain objects)."""
        return {
            "task_type": self.task_type,
            "seeded": self.seeded,
            "n_models": self.n_models,
            "quorum": self.quorum,
            "reason": self.reason,
            "consensus_chains": [c.tools for c in self.chains],
            "provenance": self.provenance,
        }


def _model_votes(per_model_chains: "dict[str, list[DecisionChain]]"):
    """For one task, reduce each model's proposals to the distinct elements it voted for.

    Each model gets ONE vote per distinct element (a model proposing the same tool twice
    still counts once), so quorum counts distinct *models*, never raw call volume.
    Returns (tools, scopes, transitions, templates, template_instances).
    """
    tool_models: dict[str, set[str]] = {}
    scope_models: dict[tuple[str, str], set[str]] = {}      # (tool, scope) -> models
    transition_models: dict[tuple[str, str], set[str]] = {}  # (a, b) -> models
    template_models: dict[tuple[str, ...], set[str]] = {}    # full ordered tool tuple
    # one representative chain per (template, model), for reconstructing scopes/args
    template_instances: dict[tuple[str, ...], dict[str, DecisionChain]] = {}

    for model, chains in per_model_chains.items():
        for chain in chains:
            tools = tuple(chain.tools)
            if not tools:
                continue
            for t in set(tools):
                tool_models.setdefault(t, set()).add(model)
            for c in chain.calls:
                if c.scope:
                    scope_models.setdefault((c.tool, c.scope), set()).add(model)
            for a, b in zip(tools, tools[1:]):
                transition_models.setdefault((a, b), set()).add(model)
            template_models.setdefault(tools, set()).add(model)
            template_instances.setdefault(tools, {}).setdefault(model, chain)

    return tool_models, scope_models, transition_models, template_models, template_instances


def _consensus_scope(tool: str, scope_models, q: int) -> str:
    """The single quorum scope for a tool (deterministic), or '' if none reaches quorum."""
    candidates = sorted(
        ((scope, len(models)) for (t, scope), models in scope_models.items() if t == tool),
        key=lambda kv: (-kv[1], kv[0]),
    )
    for scope, votes in candidates:
        if votes >= q:
            return scope
    return ""


def _build_chain(task: str, tools: tuple[str, ...], instances: "dict[str, DecisionChain]",
                 scope_models, q: int) -> DecisionChain:
    """Synthesize one consensus chain from a surviving template.

    Tools come from the quorum template (so order is one a quorum actually proposed); each
    call's scope/args are taken only if they too reach quorum, else left structural-empty.
    """
    rep = next(iter(instances.values()))
    rep_by_pos = {i: c for i, c in enumerate(rep.calls)}
    calls: list[ToolCall] = []
    for i, t in enumerate(tools):
        src = rep_by_pos.get(i)
        scope = _consensus_scope(t, scope_models, q)
        calls.append(ToolCall(
            tool=t,
            scope=scope,
            arguments=dict(src.arguments) if src else {},
            category=src.category if src else "",
            risk=src.risk if src else 0,
        ))
    return DecisionChain(task_type=task, calls=calls, runtime="consensus")


def build_consensus(
    proposals: "dict[str, dict[str, list[DecisionChain]]]",
    *,
    quorum: int | None = None,
    min_models: int = 2,
) -> "dict[str, ConsensusResult]":
    """Distill model proposals into a per-task consensus seed.

    Args:
        proposals: {task -> {model -> [DecisionChain]}}.
        quorum: override the per-task quorum; default `max(2, ceil(N/2))`.
        min_models: refuse to seed a task answered by fewer than this many models (TC-F-19).
    """
    results: dict[str, ConsensusResult] = {}

    for task, per_model in proposals.items():
        answering = [m for m, chains in per_model.items() if any(c.tools for c in chains)]
        n = len(answering)

        if n < min_models:
            results[task] = ConsensusResult(
                task_type=task, seeded=False, n_models=n, quorum=0,
                reason=f"only {n} model(s) answered; need >= {min_models} (TC-F-19)",
                provenance={"answering_models": sorted(answering)},
            )
            continue

        q = quorum if quorum is not None else quorum_for(n)
        tool_m, scope_m, trans_m, tmpl_m, tmpl_inst = _model_votes(per_model)

        surviving_templates = sorted(
            (t for t, models in tmpl_m.items() if len(models) >= q),
            key=lambda t: (-len(tmpl_m[t]), t),
        )

        chains: list[DecisionChain] = []
        if surviving_templates:
            # Template-first: emit each quorum chain-template as-is. A combined chain no
            # model proposed has template count 0, so it can never appear here (TC-F-28).
            for tmpl in surviving_templates:
                chains.append(_build_chain(task, tmpl, tmpl_inst[tmpl], scope_m, q))
        else:
            # Fallback: no full template reached quorum — reconstruct maximal ordered paths
            # from quorum transitions among quorum tools. Still order-faithful: only
            # transitions a quorum of models produced are walked.
            fb = _reconstruct_from_transitions(task, tool_m, trans_m, scope_m, q)
            chains.extend(fb)

        seeded = bool(chains)
        results[task] = ConsensusResult(
            task_type=task,
            seeded=seeded,
            n_models=n,
            quorum=q,
            chains=chains,
            reason="" if seeded else "no element reached quorum",
            provenance={
                "answering_models": sorted(answering),
                "tool_votes": {t: len(m) for t, m in sorted(tool_m.items())},
                "transition_votes": {f"{a}->{b}": len(m)
                                     for (a, b), m in sorted(trans_m.items())},
                "template_votes": {"->".join(t): len(m)
                                   for t, m in sorted(tmpl_m.items())},
                "surviving_templates": ["->".join(t) for t in surviving_templates],
            },
        )

    return results


def _reconstruct_from_transitions(task, tool_m, trans_m, scope_m, q) -> list[DecisionChain]:
    """Greedy maximal-path reconstruction over quorum transitions (template fallback)."""
    quorum_tools = {t for t, m in tool_m.items() if len(m) >= q}
    quorum_edges = {(a, b) for (a, b), m in trans_m.items()
                    if len(m) >= q and a in quorum_tools and b in quorum_tools}
    if not quorum_edges:
        # No quorum ordering at all — fold quorum tools as singleton chains so individually
        # agreed tools still count as known, without inventing any ordering.
        return [DecisionChain(task_type=task, runtime="consensus",
                              calls=[ToolCall(tool=t, scope=_consensus_scope(t, scope_m, q))])
                for t in sorted(quorum_tools)]

    succ: dict[str, list[str]] = {}
    for a, b in quorum_edges:
        succ.setdefault(a, []).append(b)
    starts = {a for a, _ in quorum_edges} - {b for _, b in quorum_edges}
    if not starts:                       # cycle — start anywhere deterministic
        starts = {sorted(a for a, _ in quorum_edges)[0]}

    chains: list[DecisionChain] = []
    for start in sorted(starts):
        path = [start]
        seen = {start}
        cur = start
        while cur in succ:
            nxt = sorted(succ[cur])[0]
            if nxt in seen:
                break
            path.append(nxt)
            seen.add(nxt)
            cur = nxt
        chains.append(DecisionChain(
            task_type=task, runtime="consensus",
            calls=[ToolCall(tool=t, scope=_consensus_scope(t, scope_m, q)) for t in path],
        ))
    return chains
