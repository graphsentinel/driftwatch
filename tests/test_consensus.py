# tests/test_consensus.py
"""Consensus aggregation tests (FR-9, R9).

TC-F-18 majority keep / minority drop, TC-F-19 single-model refusal, TC-F-28 a combined
chain no single model proposed must NOT enter the baseline (multi-granularity quorum).
Pure — no Ollama, no cluster, no network.
"""
from driftwatch.consensus import build_consensus, quorum_for
from driftwatch.operator.policy import validate
from driftwatch.operator.reconcile import Reconciler
from driftwatch.sdk.observation import DecisionChain, ToolCall


def _chain(task, *tools):
    """tools: each is a bare name or a (tool, scope) tuple."""
    c = DecisionChain(task_type=task)
    for t in tools:
        if isinstance(t, tuple):
            c.add(ToolCall(tool=t[0], scope=t[1]))
        else:
            c.add(ToolCall(tool=t))
    return c


def test_quorum_math():
    # floor of 2 (a single voice never defines normal), scaling with ceil(N/2)
    assert quorum_for(2) == 2
    assert quorum_for(3) == 2
    assert quorum_for(4) == 2
    assert quorum_for(5) == 3
    assert quorum_for(6) == 3


def test_tc_f_28_combined_chain_no_model_proposed_is_excluded():
    # Each tool is individually majority, but the COMBINED chain
    # [readLogs, scaleUp, restartPod] was proposed by no model.
    proposals = {"investigate_latency": {
        "m1": [_chain("investigate_latency", "readLogs", "scaleUp")],
        "m2": [_chain("investigate_latency", "readLogs", "scaleUp")],
        "m3": [_chain("investigate_latency", "readLogs", "restartPod")],
        "m4": [_chain("investigate_latency", "readLogs", "restartPod")],
    }}
    r = build_consensus(proposals)["investigate_latency"]
    assert r.seeded and r.quorum == 2 and r.n_models == 4

    templates = {tuple(c.tools) for c in r.chains}
    # the two real, quorum chain-templates survive...
    assert ("readLogs", "scaleUp") in templates
    assert ("readLogs", "restartPod") in templates
    # ...but the synthetic combined chain never appears
    assert ("readLogs", "scaleUp", "restartPod") not in templates
    # and the cross transition scaleUp->restartPod (proposed by nobody) is in no chain
    cross = any((a, b) == ("scaleUp", "restartPod")
                for c in r.chains for a, b in zip(c.tools, c.tools[1:]))
    assert not cross
    # provenance shows the transition got zero quorum
    assert r.provenance["transition_votes"].get("scaleUp->restartPod") is None


def test_tc_f_18_majority_keep_minority_drop():
    proposals = {"t": {
        "m1": [_chain("t", "A", "B")],
        "m2": [_chain("t", "A", "B")],
        "m3": [_chain("t", "A", "B")],
        "m4": [_chain("t", "A", "DeleteNamespace")],  # only 1 model
    }}
    r = build_consensus(proposals)["t"]
    tools = {tt for c in r.chains for tt in c.tools}
    assert {"A", "B"} <= tools          # proposed by >= 2 models
    assert "DeleteNamespace" not in tools  # single-model -> dropped


def test_tc_f_19_single_model_refused():
    proposals = {"rare_task": {"m1": [_chain("rare_task", "X", "Y")]}}
    r = build_consensus(proposals)["rare_task"]
    assert r.seeded is False
    assert r.chains == []
    assert "need >= 2" in r.reason       # recorded for consensus_seed.json


def test_empty_proposals_dont_count_as_a_voice():
    # a model that returns nothing for a task must not count toward N
    proposals = {"t": {
        "m1": [_chain("t", "A", "B")],
        "m2": [_chain("t")],            # empty
    }}
    r = build_consensus(proposals)["t"]
    assert r.n_models == 1 and r.seeded is False


def test_consensus_seed_folds_into_a_ready_baseline():
    # End-to-end: the consensus chains seed a Reconciler baseline (TC-F-15 seam).
    proposals = {"investigate_latency": {
        "m1": [_chain("investigate_latency", ("readLogs", "ns/app"), "scaleUp")],
        "m2": [_chain("investigate_latency", ("readLogs", "ns/app"), "scaleUp")],
        "m3": [_chain("investigate_latency", ("readLogs", "ns/app"), "restartPod")],
        "m4": [_chain("investigate_latency", ("readLogs", "ns/app"), "restartPod")],
    }}
    seed = {task: r.chains for task, r in build_consensus(proposals).items() if r.seeded}

    spec = {"action": "block",
            "baseline": {"sources": [{"models": ["m1", "m2", "m3", "m4"]}], "window": 50},
            "detection": {"features": ["tool", "scope", "sequence", "argSchemaHash"],
                          "threshold": 3.0}}
    rec = Reconciler(validate(spec))
    rec.seed_from_models(seed)

    b = rec.store.get("investigate_latency")
    assert b.ready
    assert b.expected_tools == {"readLogs", "scaleUp", "restartPod"}
    assert "ns/app" in b.seen_scopes      # quorum scope carried through


def test_seed_from_proposals_writes_provenance(tmp_path):
    # T-C3/T-C4: offline driver folds survivors + writes consensus_seed.json.
    from driftwatch.consensus.seed import seed_from_proposals

    proposals = {
        "investigate_latency": {
            "m1": [["readLogs", "scaleUp"]], "m2": [["readLogs", "scaleUp"]],
            "m3": [["readLogs", "restartPod"]], "m4": [["readLogs", "restartPod"]],
        },
        "rare": {"m1": [["x"]]},
    }
    spec = {"action": "block",
            "baseline": {"sources": [{"models": ["m1", "m2", "m3", "m4"]}], "window": 50},
            "detection": {"features": ["tool", "scope", "sequence", "argSchemaHash"],
                          "threshold": 3.0}}
    results, rec = seed_from_proposals(proposals, spec, out_dir=str(tmp_path))

    assert results["investigate_latency"].seeded
    assert results["rare"].seeded is False
    assert rec.store.get("investigate_latency").ready

    import json
    record = json.loads((tmp_path / "consensus_seed.json").read_text())
    assert record["rare"]["seeded"] is False
    assert ["readLogs", "scaleUp"] in record["investigate_latency"]["consensus_chains"]


def test_provenance_records_every_level():
    proposals = {"t": {
        "m1": [_chain("t", "A", "B")],
        "m2": [_chain("t", "A", "B")],
    }}
    r = build_consensus(proposals)["t"]
    p = r.provenance
    assert set(p) >= {"answering_models", "tool_votes", "transition_votes",
                      "template_votes", "surviving_templates"}
    assert p["tool_votes"]["A"] == 2
    assert p["transition_votes"]["A->B"] == 2
