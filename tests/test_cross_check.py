"""E13 §4c — LLM cross-check detector (prompt-aware second opinion). Pure unit tests, LLM mocked."""
from __future__ import annotations

import driftwatch.interceptor.cross_check as cc

CANDS = ("pods_list", "pods_delete", "nodes_top")


class _Resp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": self._c}}


def _mock_answers(monkeypatch, answers):
    """httpx.post returns the next answer each call (cycles); records prompts seen."""
    seq = list(answers)
    calls = {"n": 0}

    def fake_post(url, json, timeout):
        i = calls["n"]
        calls["n"] += 1
        return _Resp(seq[i % len(seq)])

    monkeypatch.setattr("httpx.post", fake_post)
    return calls


# --- predict_expected_tool ---

def test_predict_matches_exact_answer(monkeypatch):
    _mock_answers(monkeypatch, ["pods_list"])
    assert cc.predict_expected_tool("list the pods", CANDS, model="m") == "pods_list"


def test_predict_matches_answer_with_extra_words(monkeypatch):
    _mock_answers(monkeypatch, ["I would call `pods_list` here."])
    assert cc.predict_expected_tool("list pods", CANDS, model="m") == "pods_list"


def test_predict_returns_none_on_no_match(monkeypatch):
    _mock_answers(monkeypatch, ["use kubectl directly"])
    assert cc.predict_expected_tool("x", CANDS, model="m") is None


def test_predict_returns_none_when_llm_unreachable(monkeypatch):
    def boom(url, json, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr("httpx.post", boom)
    assert cc.predict_expected_tool("x", CANDS, model="m") is None


def test_predict_none_on_empty_prompt_or_candidates(monkeypatch):
    _mock_answers(monkeypatch, ["pods_list"])
    assert cc.predict_expected_tool("", CANDS, model="m") is None
    assert cc.predict_expected_tool("x", [], model="m") is None


# --- cross_check (divergence + N-vote) ---

def test_no_divergence_when_observed_matches_prediction(monkeypatch):
    _mock_answers(monkeypatch, ["pods_list"])
    r = cc.cross_check("list pods", "pods_list", CANDS, model="m")
    assert r.diverged is False
    assert r.predicted == ("pods_list",)


def test_divergence_when_observed_differs_from_prediction(monkeypatch):
    _mock_answers(monkeypatch, ["pods_list"])
    r = cc.cross_check("list the pods", "pods_delete", CANDS, model="m")   # agent deleted, predicted list
    assert r.diverged is True
    assert r.observed == "pods_delete" and r.predicted == ("pods_list",)


def test_majority_vote_decides_winner(monkeypatch):
    # 3 votes: pods_list, pods_list, pods_delete → winner pods_list; observed pods_delete → diverged
    _mock_answers(monkeypatch, ["pods_list", "pods_list", "pods_delete"])
    r = cc.cross_check("list pods", "pods_delete", CANDS, model="m", votes=3)
    assert r.predicted == ("pods_list",)
    assert r.votes == 3
    assert abs(r.confidence - 2 / 3) < 1e-9
    assert r.diverged is True


def test_empty_panel_is_no_signal_not_a_false_positive(monkeypatch):
    _mock_answers(monkeypatch, ["totally unrelated"])      # never matches a candidate → no prediction
    r = cc.cross_check("x", "pods_delete", CANDS, model="m", votes=2)
    assert r.votes == 0 and r.diverged is False            # no usable vote → no signal


# --- config gate ---

def test_cross_check_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DRIFTWATCH_CROSS_CHECK_ENABLED", raising=False)
    assert cc.cross_check_enabled() is False


def test_cross_check_enabled_via_env(monkeypatch):
    monkeypatch.setenv("DRIFTWATCH_CROSS_CHECK_ENABLED", "true")
    assert cc.cross_check_enabled() is True


# --- engine integration: shadow cross-check is emit-only (never changes the verdict) ---

def _ready_store(task="investigate_latency"):
    from driftwatch.library.baseline import BaselineStore
    from driftwatch.sdk import DecisionChain, ToolCall
    store = BaselineStore(window=10)
    for _ in range(3):
        c = DecisionChain(task_type=task)
        c.add(ToolCall(tool="QueryMetrics", scope="checkout", category="observability"))
        c.add(ToolCall(tool="QueryLogs", scope="checkout", category="observability"))
        store.fold(c)
    return store


def _diverged(*a, **k):
    return cc.CrossCheckResult(True, ("QueryLogs",), "QueryMetrics", 1, 1.0)


def test_engine_shadow_cross_check_emits_without_changing_verdict(monkeypatch):
    from driftwatch.adapters import KagentAdapter
    from driftwatch.interceptor import FORWARD, Interceptor
    from driftwatch.otel.emit import Emitter
    monkeypatch.setenv("DRIFTWATCH_CROSS_CHECK_ENABLED", "true")
    monkeypatch.setattr(cc, "cross_check", _diverged)   # force a divergence

    seen = []

    class _Spy(Emitter):
        def emit_cross_check(self, chain, tool, **kw):
            seen.append((tool, kw))
            return {}

    adapter = KagentAdapter(task_type="investigate_latency")
    itc = Interceptor(_ready_store(), adapter, action="block", emitter=_Spy(), cc_model="m")
    v = itc.handle({"tool": "QueryMetrics", "namespace": "checkout",       # within baseline → FORWARD
                    "meta": {"prompt": "investigate latency", "tools": ["QueryMetrics", "QueryLogs"]}})
    assert v.outcome == FORWARD                       # shadow: verdict unchanged
    assert seen and seen[0][0] == "QueryMetrics"      # divergence emitted as a signal
    assert seen[0][1]["predicted"] == ("QueryLogs",)


def test_engine_cross_check_off_by_default_no_emit(monkeypatch):
    from driftwatch.adapters import KagentAdapter
    from driftwatch.interceptor import Interceptor
    from driftwatch.otel.emit import Emitter
    monkeypatch.delenv("DRIFTWATCH_CROSS_CHECK_ENABLED", raising=False)
    monkeypatch.setattr(cc, "cross_check", _diverged)

    seen = []

    class _Spy(Emitter):
        def emit_cross_check(self, chain, tool, **kw):
            seen.append(tool)
            return {}

    adapter = KagentAdapter(task_type="investigate_latency")
    itc = Interceptor(_ready_store(), adapter, action="block", emitter=_Spy(), cc_model="m")
    itc.handle({"tool": "QueryMetrics", "namespace": "checkout",
                "meta": {"prompt": "x", "tools": ["QueryMetrics"]}})
    assert seen == []                                  # disabled → no LLM, no signal


def test_cross_check_total_deadline_caps_votes(monkeypatch):
    # consultant #4: a HARD total deadline across votes — votes=10 with slow predicts must NOT run all
    import time
    calls = {"n": 0}

    def slow_post(url, json, timeout):
        calls["n"] += 1
        time.sleep(0.3)
        return _Resp("pods_list")

    monkeypatch.setattr("httpx.post", slow_post)
    r = cc.cross_check("x", "pods_list", CANDS, model="m", votes=10, total_timeout=0.5)
    assert calls["n"] < 10            # deadline stopped voting early
    assert r.votes <= calls["n"]      # never more usable votes than calls made


def test_predict_openai_compatible_runpod(monkeypatch):
    # cross-check light LLM can also be openai-compatible (OpenAI/Azure/RunPod/vLLM) — RunPod = endpoint
    import driftwatch.interceptor.cross_check as ccm
    captured = {}

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "pods_list"}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return _R()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-x")
    p = ccm.predict_expected_tool("list pods", ("pods_list", "pods_get"), model="m",
                                  provider="runpod", endpoint="https://api.runpod.ai/v2/x/openai/v1")
    assert p == "pods_list"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer rp-x"
