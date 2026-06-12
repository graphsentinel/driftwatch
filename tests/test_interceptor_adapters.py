"""S3 tests — adapters (TC-F-09/10), enforcement actions (TC-F-05/06/07), resilience (TC-F-11)."""
import pytest

from driftwatch.adapters import GooseAdapter, KagentAdapter
from driftwatch.interceptor import BLOCK, DROP, FORWARD, Interceptor
from driftwatch.library.baseline import BaselineStore
from driftwatch.sdk import DecisionChain, RuntimeAdapter, ToolCall


def _ready_store(task="investigate_latency"):
    # scope must match what KagentAdapter produces from {"namespace": "checkout"},
    # i.e. "checkout" — the interceptor path normalizes through the adapter.
    store = BaselineStore(window=10)
    for _ in range(3):
        c = DecisionChain(task_type=task)
        c.add(ToolCall(tool="QueryMetrics", scope="checkout", category="observability"))
        c.add(ToolCall(tool="QueryLogs", scope="checkout", category="observability"))
        store.fold(c)
    return store


# --- adapters ---

def test_kagent_and_goose_normalize_same_shape():  # TC-F-09
    k = KagentAdapter(task_type="t").normalize({"tool": "DeleteNamespace", "namespace": "checkout"})
    g = GooseAdapter(task_type="t").normalize({"name": "DeleteNamespace",
                                               "parameters": {"namespace": "checkout"}})
    assert (k.tool, k.scope) == (g.tool, g.scope) == ("DeleteNamespace", "checkout")


def test_custom_adapter_resolves_by_name():  # TC-F-10
    cls = RuntimeAdapter.get("custom-example")
    call = cls(task_type="t").normalize({"function": {"name": "Scale", "arguments": {"replicas": 3}}})
    assert call.tool == "Scale" and call.arguments["replicas"] == 3
    assert RuntimeAdapter.get("builtin/kagent") is KagentAdapter


# --- enforcement actions (drifting 2nd call against a ready baseline) ---

def _attack_interceptor(action):
    adapter = KagentAdapter(task_type="investigate_latency")
    adapter.observe({"tool": "QueryMetrics", "namespace": "checkout"})  # normal first call
    return Interceptor(_ready_store(), adapter, threshold=3.0, action=action)


def test_action_log_forwards():  # TC-F-05
    v = _attack_interceptor("log").handle({"tool": "DeleteNamespace", "namespace": "checkout"})
    assert v.outcome == FORWARD and v.http_status == 200
    assert v.decision.gate_action == "log"
    assert v.signals["span"]["gen_ai.agent.gate.blocked"] is False


def test_action_drop_silent():  # TC-F-06
    v = _attack_interceptor("drop").handle({"tool": "DeleteNamespace", "namespace": "checkout"})
    assert v.outcome == DROP and v.http_status == 200
    assert v.decision.gate_action == "drop" and v.decision.gate_blocked is True


def test_action_block_403():  # TC-F-07
    v = _attack_interceptor("block").handle({"tool": "DeleteNamespace", "namespace": "checkout"})
    assert v.outcome == BLOCK and v.http_status == 403
    assert v.signals["span"]["gen_ai.agent.gate.action"] == "block"


def test_happy_path_forwards():
    adapter = KagentAdapter(task_type="investigate_latency")
    adapter.observe({"tool": "QueryMetrics", "namespace": "checkout"})
    v = Interceptor(_ready_store(), adapter, action="block").handle(
        {"tool": "QueryLogs", "namespace": "checkout"})
    assert v.outcome == FORWARD and v.http_status == 200


# --- E11: declared contract (configure/declare) check, alongside the statistical baseline ---

def _contract():
    from driftwatch.library.contract import build_contract
    return build_contract({
        "tools": [{"name": "DeleteNamespace", "risk": 4}],
        "agents": [{"name": "agent-x", "tools": ["QueryMetrics", "QueryLogs"],
                    "scope": ["checkout"]}],
    })


def test_declared_violation_unbound_tool_blocks():  # E11 (+ consultant #3 OTel)
    # DeleteNamespace is not bound to agent-x → declared violation, blocked before scoring.
    adapter = KagentAdapter(task_type="investigate_latency", agent_id="agent-x")
    itc = Interceptor(_ready_store(), adapter, action="block", contract=_contract())
    v = itc.handle({"tool": "DeleteNamespace", "namespace": "checkout"})
    assert v.outcome == BLOCK and v.http_status == 403
    assert v.signals.get("declared") is True and "not bound" in v.signals["reason"]
    # consultant #3: declared violation emits the gen_ai.agent.* schema, distinguishable via
    # gate.declared=true, with the same gate.action/blocked attributes as a statistical drift.
    span = v.signals["span"]
    assert span["gen_ai.agent.gate.declared"] is True
    assert span["gen_ai.agent.gate.action"] == "block" and span["gen_ai.agent.gate.blocked"] is True
    assert span["gen_ai.tool.name"] == "DeleteNamespace"
    assert span["gen_ai.agent.computed.anomaly.kind"] == "declared_violation"
    assert span["gen_ai.agent.tool.risk_severity"] == 4   # from the contract risk_map


def test_declared_out_of_scope_blocks():  # E11
    # QueryMetrics is bound, but only in scope "checkout"; "prod" is out of declared scope.
    adapter = KagentAdapter(task_type="investigate_latency", agent_id="agent-x")
    itc = Interceptor(_ready_store(), adapter, action="block", contract=_contract())
    v = itc.handle({"tool": "QueryMetrics", "namespace": "prod"})
    assert v.outcome == BLOCK and v.signals.get("declared") is True and "scope" in v.signals["reason"]


def test_declared_within_contract_falls_through_to_statistical():  # E11
    # Bound tool, in scope, within baseline → declared check passes, statistical passes → FORWARD.
    adapter = KagentAdapter(task_type="investigate_latency", agent_id="agent-x")
    adapter.observe({"tool": "QueryMetrics", "namespace": "checkout"})
    itc = Interceptor(_ready_store(), adapter, action="block", contract=_contract())
    v = itc.handle({"tool": "QueryLogs", "namespace": "checkout"})
    assert v.outcome == FORWARD and v.http_status == 200


def test_declared_check_blocks_even_before_baseline_ready():  # E11
    # Declared violation is deterministic — gated regardless of baseline readiness (cold start).
    from driftwatch.library.baseline import BaselineStore
    adapter = KagentAdapter(task_type="investigate_latency", agent_id="agent-x")
    itc = Interceptor(BaselineStore(window=10), adapter, action="block", contract=_contract())
    v = itc.handle({"tool": "DeleteNamespace", "namespace": "checkout"})
    assert v.outcome == BLOCK and v.signals.get("declared") is True


def _contract_with_rule():
    from driftwatch.library.contract import build_contract
    return build_contract({
        "agents": [{"name": "agent-x", "tools": ["QueryMetrics", "QueryLogs", "DeleteNamespace"]}],
        "rules": [{"deny": ["QueryMetrics", "DeleteNamespace"], "reason": "recon-then-destroy"}],
    })


def test_declared_sequence_blocks():  # E12
    # QueryMetrics then DeleteNamespace is a declared deny-sequence (both bound, so it's the
    # SEQUENCE that's forbidden, not the single call) → blocked before scoring.
    adapter = KagentAdapter(task_type="investigate_latency", agent_id="agent-x")
    adapter.observe({"tool": "QueryMetrics", "namespace": "checkout"})   # first call
    itc = Interceptor(_ready_store(), adapter, action="block", contract=_contract_with_rule())
    v = itc.handle({"tool": "DeleteNamespace", "namespace": "checkout"})  # completes the deny tail
    assert v.outcome == BLOCK and v.http_status == 403
    assert v.signals.get("declared") is True and "deny-sequence" in v.signals["reason"]
    assert v.signals["span"]["gen_ai.agent.computed.anomaly.kind"] == "declared_sequence"


def test_no_contract_is_standalone_unchanged():  # E11 standalone == E1–E10
    # With no contract, an unbound/out-of-scope call is irrelevant; pure statistical path applies.
    adapter = KagentAdapter(task_type="investigate_latency", agent_id="agent-x")
    adapter.observe({"tool": "QueryMetrics", "namespace": "checkout"})
    v = Interceptor(_ready_store(), adapter, action="block").handle(  # contract=None
        {"tool": "QueryLogs", "namespace": "checkout"})
    assert v.outcome == FORWARD and v.http_status == 200


# --- resilience (TC-F-11) ---

class _BoomAdapter(RuntimeAdapter):
    name = "boom"

    def normalize(self, raw):
        raise RuntimeError("interceptor blew up mid-session")


@pytest.mark.parametrize("policy,expected,status", [
    ("failClosed", BLOCK, 403),
    ("failOpen", FORWARD, 200),
])
def test_failure_policy(policy, expected, status):  # TC-F-11
    itc = Interceptor(_ready_store(), _BoomAdapter(task_type="investigate_latency"),
                      action="block", failure_policy=policy)
    v = itc.handle({"tool": "anything"})
    assert v.outcome == expected and v.http_status == status


def test_cold_start_failclosed_blocks():  # TC-F-04 at runtime
    itc = Interceptor(BaselineStore(window=10), KagentAdapter(task_type="new_task"),
                      action="block", failure_policy="failClosed")
    v = itc.handle({"tool": "QueryMetrics", "namespace": "x"})
    assert v.outcome == BLOCK


# --- HTTP layer (server.py) — guards the FastAPI wiring the image actually runs ---

def _http(method: str, path: str, **kw):
    # Drive the ASGI app via httpx.ASGITransport on a fresh event loop per call — NOT Starlette's
    # TestClient. TestClient bridges sync→async through an anyio portal thread; under the full suite
    # (+ pytest-timeout's thread method) that portal intermittently deadlocked even when used as a
    # context manager. ASGITransport speaks ASGI directly (no portal, no background thread), so each
    # test is hermetic.
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from driftwatch.interceptor.server import build_app
    adapter = KagentAdapter(task_type="investigate_latency")
    adapter.observe({"tool": "QueryMetrics", "namespace": "checkout"})  # normal first call
    app = build_app(Interceptor(_ready_store(), adapter, action="block"))

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(_go())


def test_http_healthz():
    assert _http("GET", "/healthz").json() == {"status": "ok"}


def test_http_tool_call_accepts_json_body_and_blocks():  # regression: was 422 (Response treated as query param)
    # the drifting call must reach the engine (not be rejected by request parsing)
    r = _http("POST", "/v1/tool-call", json={"tool": "DeleteNamespace", "namespace": "checkout"})
    assert r.status_code == 403
    assert r.json()["outcome"] == "block"


def test_http_tool_call_forwards_within_baseline():
    r = _http("POST", "/v1/tool-call", json={"tool": "QueryLogs", "namespace": "checkout"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "forward"


# --- control->data-plane handoff (FR-10 / R1) ---

def test_sidecar_loads_reconciled_baseline_and_policy(tmp_path, monkeypatch):  # TC-F-20
    """The sidecar must enforce the operator-reconciled baseline + policy, not an empty
    store with hard-coded defaults."""
    from driftwatch.db.sqlite import SqliteBackend
    from driftwatch.interceptor.main import build_default_interceptor

    # operator side: reconcile a baseline and persist it to the shared store
    monkeypatch.setenv("DRIFTWATCH_DATA_DIR", str(tmp_path))
    store = SqliteBackend().load(window=50)
    for _ in range(3):
        c = DecisionChain(task_type="investigate_latency")
        c.add(ToolCall(tool="QueryMetrics", scope="ns/a", category="observability"))
        c.add(ToolCall(tool="QueryLogs", scope="ns/a", category="observability"))
        store.fold(c)
    SqliteBackend().save(store)

    # operator delivers the policy knobs via env
    monkeypatch.setenv("DRIFTWATCH_ACTION", "block")
    monkeypatch.setenv("DRIFTWATCH_THRESHOLD", "3.0")
    monkeypatch.setenv("DRIFTWATCH_FEATURES", "tool,scope,sequence,argSchemaHash")

    # sidecar side: build from env — must NOT be empty
    itc = build_default_interceptor()
    assert "investigate_latency" in itc.store.task_types()  # loaded, not empty
    assert itc.action == "block" and itc.threshold == 3.0

    # and it enforces against that baseline: a drifting call is blocked
    itc.adapter = KagentAdapter(task_type="investigate_latency")
    itc.adapter.observe({"tool": "QueryMetrics", "namespace": "a"})
    v = itc.handle({"tool": "DeleteNamespace", "namespace": "a"})
    assert v.outcome == BLOCK and v.http_status == 403


def test_sidecar_cold_start_when_no_store(monkeypatch):  # FR-10 cold-start path
    from driftwatch.interceptor.main import build_default_interceptor
    for k in ["DRIFTWATCH_DATA_DIR", "DRIFTWATCH_ACTION", "DRIFTWATCH_THRESHOLD",
              "DRIFTWATCH_FEATURES", "DRIFTWATCH_FAILURE_POLICY"]:
        monkeypatch.delenv(k, raising=False)
    itc = build_default_interceptor()
    assert itc.store.task_types() == []          # empty store, no crash
    assert itc.action == "block" and itc.failure_policy == "failClosed"  # safe defaults


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_sidecar_reads_baseline_from_readonly_store(tmp_path, monkeypatch):  # TC-F-21 (unit)
    """Sidecar loads the operator baseline from a READ-ONLY store mount, not cold-start.

    Simulates the in-cluster readOnly PVC: after the operator writes the baseline the data
    dir is made read-only, so a *writing* SqliteBackend (mkdir / CREATE TABLE) would fail.
    build_default_interceptor must still load it via the read_only path and enforce.
    """
    import os
    import stat

    import driftwatch.interceptor.main as main
    from driftwatch.operator.policy import validate
    from driftwatch.operator.reconcile import Reconciler
    from driftwatch.sdk.observation import DecisionChain, ToolCall

    monkeypatch.setenv("DRIFTWATCH_DATA_DIR", str(tmp_path))
    spec = {
        "action": "block",
        "baseline": {"sources": ["successfulRuns"], "window": 10},
        "detection": {"features": ["tool", "scope", "sequence", "argSchemaHash"]},
    }
    rec = Reconciler(validate({**spec, "_name": "p"}), persistent=True)
    for _ in range(4):
        ch = DecisionChain(task_type="investigate_latency")
        ch.add(ToolCall(tool="QueryMetrics", scope="ns/app"))
        ch.add(ToolCall(tool="QueryLogs", scope="ns/app"))
        rec.observe(ch, source="successfulRuns")

    # make the data dir tree read-only (owner loses write) to mimic a readOnly mount
    paths = []
    for root, _dirs, files in os.walk(str(tmp_path)):
        for f in files:
            paths.append(os.path.join(root, f))
        paths.append(root)
    for f in [p for p in paths if os.path.isfile(p)]:
        os.chmod(f, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    for d in [p for p in paths if os.path.isdir(p)]:
        os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)

    try:
        itc = main.build_default_interceptor()
        base = itc.store.get("investigate_latency")
        assert base.ready, "sidecar cold-started instead of loading the read-only baseline"
        assert "QueryMetrics" in base.expected_tools
        # the default adapter has task_type="" so calls score under the wrong baseline;
        # point it at the seeded task (the real sidecar's adapter is task-scoped) and reset
        # between calls so each is judged as its own chain, like a fresh tool-call hop.
        itc.adapter = KagentAdapter(task_type="investigate_latency")
        itc.adapter.reset()
        assert itc.handle(
            {"tool": "QueryMetrics", "namespace": "ns/app"}
        ).outcome == "forward"
        itc.adapter.reset()
        assert itc.handle(
            {"tool": "DeleteNamespace", "namespace": "ns/app"}
        ).outcome in ("block", "drop")
    finally:
        # restore perms so pytest can clean up tmp_path
        for d in [p for p in paths if os.path.isdir(p)]:
            os.chmod(d, stat.S_IRWXU)
        for f in [p for p in paths if os.path.isfile(p)]:
            os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)


def test_http_register_contract_hot_reloads(tmp_path, monkeypatch):
    # E13 §4e — AgentGate pushes its declared contract; the interceptor stores + hot-reloads it
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from driftwatch.library.contract import build_contract
    from driftwatch.interceptor.server import build_app
    monkeypatch.setenv("DRIFTWATCH_DATA_DIR", str(tmp_path))   # writable, no cwd side-effect
    itc = Interceptor(_ready_store(), KagentAdapter(task_type="t"), action="block")
    assert itc.contract is None                                # nothing declared yet
    app = build_app(itc)
    body = {"source": "agentgate",
            "contract": build_contract({"agents": [{"name": "ops", "tools": ["pods_list"]}]}).to_dict()}

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/contracts", json=body)

    r = asyncio.run(_go())
    assert r.status_code == 200
    assert r.json()["source"] == "agentgate"
    assert itc.contract is not None and "ops" in itc.contract.agents   # hot-reloaded → governs now


# --- multi-app: central DriftWatch, N AgentGates (registry + _meta routing) ---

def _app_contract(agent, tools):
    from driftwatch.library.contract import build_contract
    return build_contract({"agents": [{"name": agent, "tools": tools}]})


def test_meta_app_routes_to_the_right_contract():
    """Two apps registered; `_meta.app` selects which app's declared contract governs the call.

    app-a binds only toolA; app-b binds only toolB. A toolB call tagged app-a is a declared violation
    (toolB unbound in app-a's contract); the SAME toolB call tagged app-b forwards. Proves routing —
    apps don't share a single contract (the old last-push-wins).
    """
    registry = {"app-a": _app_contract("worker", ["toolA"]),
                "app-b": _app_contract("worker", ["toolB"])}
    # one shared proxy seat (no env agent_id); identity comes from _meta per call
    itc = Interceptor(_ready_store(), KagentAdapter(task_type="t"), action="block",
                      contracts=registry)
    # toolB tagged app-a → unbound in app-a's contract → declared violation
    v = itc.handle({"tool": "toolB", "args": {},
                    "meta": {"app": "app-a", "agent": "worker"}})
    assert v.outcome == BLOCK and v.signals.get("declared") is True and "not bound" in v.signals["reason"]

    # the SAME toolB tagged app-b → bound there → NOT a declared violation (routing picked app-b's
    # contract). It then falls through to the statistical layer (asserting it's not a declared block
    # isolates the routing decision from baseline state).
    itc.adapter.reset()
    v = itc.handle({"tool": "toolB", "args": {},
                    "meta": {"app": "app-b", "agent": "worker"}})
    assert v.signals.get("declared") is not True


def test_unknown_app_is_blocked_strict():
    # STRICT (consultant): with a populated registry, a call tagged with an UNREGISTERED app is
    # blocked as unknown_app — it must NOT fall back to another app's contract. toolA is bound in
    # both contracts, so the block is purely the unknown app, not a tool-binding violation.
    itc = Interceptor(_ready_store(), KagentAdapter(task_type="t", agent_id="worker"),
                      action="block", contract=_app_contract("worker", ["toolA"]),
                      contracts={"app-a": _app_contract("worker", ["toolA"])})
    v = itc.handle({"tool": "toolA", "args": {}, "meta": {"app": "ghost", "agent": "worker"}})
    assert v.outcome == BLOCK and v.signals.get("declared") is True
    assert "unknown app" in v.signals["reason"]
    assert v.signals["span"]["gen_ai.agent.computed.anomaly.kind"] == "unknown_app"


def test_metaless_call_falls_back_to_default_even_with_registry():
    # No _meta.app at all → legacy/default contract (sidecar/single-app path), even with a registry.
    itc = Interceptor(_ready_store(), KagentAdapter(task_type="t", agent_id="worker"),
                      action="block", contract=_app_contract("worker", ["toolA"]),
                      contracts={"app-a": _app_contract("worker", ["toolB"])})
    # default binds toolA; toolB with NO meta → declared violation against the DEFAULT contract
    v = itc.handle({"tool": "toolB", "args": {}})
    assert v.outcome == BLOCK and v.signals.get("declared") is True and "not bound" in v.signals["reason"]


def test_apply_contract_push_rejects_unsafe_ref(tmp_path, monkeypatch):
    # consultant MAJOR: ref becomes a filename → path traversal / bad input must be rejected (400)
    monkeypatch.setenv("DRIFTWATCH_DATA_DIR", str(tmp_path))
    from driftwatch.interceptor.server import apply_contract_push
    itc = Interceptor(_ready_store(), KagentAdapter(task_type="t"), action="block")
    good = _app_contract("w", ["toolA"]).to_dict()
    for bad in ["../etc/passwd", "a/b", "..", "/abs", "has space", "x" * 65, ".hidden"]:
        status, body = apply_contract_push(itc, {"ref": bad, "contract": good})
        assert status == 400 and "invalid ref" in body["error"], bad
    assert not itc.contracts   # nothing was stored from a rejected push


def test_valid_ref_whitelist():
    from driftwatch.library.contract import valid_ref
    assert valid_ref("app-a") and valid_ref("baseline.v1") and valid_ref("checkout_1")
    assert valid_ref("a") and valid_ref("A0")
    for bad in ["../x", "a/b", "a\\b", "..", "/abs", ".hidden", "has space", "", "x" * 65]:
        assert not valid_ref(bad), bad


def test_save_contract_rejects_path_traversal(tmp_path):
    from driftwatch.library.contract import save_contract
    with pytest.raises(ValueError, match="invalid contract ref"):
        save_contract(_app_contract("w", ["t"]), str(tmp_path), "../escape")


def test_meta_agent_overrides_seat_agent_id():
    """`_meta.agent` (the agent actually calling) drives the declared check, not the env seat.

    Contract: worker binds toolA, admin binds toolB. Seat agent_id="worker". A toolA call tagged
    agent="admin" is a declared violation (toolA unbound to admin) — proving the per-call meta identity
    wins over the seat. The same toolA WITHOUT meta (seat=worker, where toolA IS bound) is not a
    declared violation, so the verdict flips with the meta — that's the discriminator.
    """
    from driftwatch.library.contract import build_contract
    contract = build_contract({"agents": [{"name": "worker", "tools": ["toolA"]},
                                          {"name": "admin", "tools": ["toolB"]}]})
    itc = Interceptor(_ready_store(), KagentAdapter(task_type="t", agent_id="worker"),
                      action="block", contract=contract)
    v = itc.handle({"tool": "toolA", "args": {}, "meta": {"agent": "admin"}})
    assert v.outcome == BLOCK and v.signals.get("declared") is True and "not bound" in v.signals["reason"]

    itc.adapter.reset()
    v2 = itc.handle({"tool": "toolA", "args": {}})   # no meta → seat "worker", toolA bound → not declared
    assert v2.signals.get("declared") is not True


def test_apply_contract_push_registers_by_ref_no_overwrite(tmp_path, monkeypatch):
    """Two apps push under their own ref → both coexist in the registry (no last-push-wins)."""
    monkeypatch.setenv("DRIFTWATCH_DATA_DIR", str(tmp_path))
    from driftwatch.interceptor.server import apply_contract_push
    itc = Interceptor(_ready_store(), KagentAdapter(task_type="t"), action="block")
    assert itc.contracts is None

    s1, b1 = apply_contract_push(itc, {"source": "agentgate", "ref": "app-a",
                                       "contract": _app_contract("w", ["toolA"]).to_dict()})
    s2, b2 = apply_contract_push(itc, {"source": "agentgate", "ref": "app-b",
                                       "contract": _app_contract("w", ["toolB"]).to_dict()})
    assert s1 == 200 and s2 == 200
    assert set(itc.contracts) == {"app-a", "app-b"}            # both kept, no overwrite
    assert b2["apps"] == ["app-a", "app-b"]
    # app-a's contract is intact after app-b's push
    assert "toolA" in itc.contracts["app-a"].agents["w"].allowed_tools
    assert "toolB" in itc.contracts["app-b"].agents["w"].allowed_tools


def test_load_all_contracts_seeds_registry(tmp_path):
    from driftwatch.library.contract import load_all_contracts, save_contract
    save_contract(_app_contract("w", ["toolA"]), str(tmp_path), "app-a")
    save_contract(_app_contract("w", ["toolB"]), str(tmp_path), "app-b")
    reg = load_all_contracts(str(tmp_path))
    assert set(reg) == {"app-a", "app-b"}
    assert "toolB" in reg["app-b"].agents["w"].allowed_tools
    assert load_all_contracts(str(tmp_path / "nope")) == {}    # missing dir → empty, no crash
