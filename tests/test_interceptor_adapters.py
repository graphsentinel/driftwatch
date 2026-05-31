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

def _client():
    pytest.importorskip("fastapi")   # skip if interceptor extra not installed
    pytest.importorskip("httpx")     # TestClient needs httpx
    from fastapi.testclient import TestClient

    from driftwatch.interceptor.server import build_app
    adapter = KagentAdapter(task_type="investigate_latency")
    adapter.observe({"tool": "QueryMetrics", "namespace": "checkout"})  # normal first call
    return TestClient(build_app(Interceptor(_ready_store(), adapter, action="block")))


def test_http_healthz():
    assert _client().get("/healthz").json() == {"status": "ok"}


def test_http_tool_call_accepts_json_body_and_blocks():  # regression: was 422 (Response treated as query param)
    # the drifting call must reach the engine (not be rejected by request parsing)
    r = _client().post("/v1/tool-call", json={"tool": "DeleteNamespace", "namespace": "checkout"})
    assert r.status_code == 403
    assert r.json()["outcome"] == "block"


def test_http_tool_call_forwards_within_baseline():
    r = _client().post("/v1/tool-call", json={"tool": "QueryLogs", "namespace": "checkout"})
    assert r.status_code == 200
    assert r.json()["outcome"] == "forward"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
