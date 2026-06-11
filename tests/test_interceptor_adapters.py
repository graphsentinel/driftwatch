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
