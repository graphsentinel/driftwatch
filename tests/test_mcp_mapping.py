# tests/test_mcp_mapping.py
"""E7 / T-E7.2 — pure MCP tools/call -> engine-dict mapping.

No FastMCP, no network. Proves the one fiddly bit (lifting a scope out of MCP `arguments`)
and that the output round-trips through the existing KagentAdapter unchanged.
"""
from driftwatch.adapters import KagentAdapter
from driftwatch.interceptor.mcp_mapping import to_engine_call


def test_namespace_lifted_from_arguments():
    assert to_engine_call("DeleteNamespace", {"namespace": "checkout", "force": True}) == {
        "tool": "DeleteNamespace",
        "namespace": "checkout",
        "args": {"namespace": "checkout", "force": True},
    }


def test_scope_is_the_fallback_for_namespace():
    out = to_engine_call("X", {"scope": "ns/a"})
    assert out["namespace"] == "ns/a"


def test_namespace_wins_over_scope_when_both_present():
    out = to_engine_call("X", {"namespace": "ns/a", "scope": "ns/b"})
    assert out["namespace"] == "ns/a"   # _SCOPE_KEYS priority order


def test_no_scope_yields_empty_namespace_but_keeps_args():
    out = to_engine_call("Scale", {"replicas": 3})
    assert out == {"tool": "Scale", "namespace": "", "args": {"replicas": 3}}


def test_full_args_preserved_for_arg_schema_hash():
    # the engine's argSchemaHash feature must see the real shape, so args is kept whole
    out = to_engine_call("Apply", {"namespace": "ns", "manifest": {"k": "v"}, "n": 1})
    assert out["args"] == {"namespace": "ns", "manifest": {"k": "v"}, "n": 1}


def test_none_and_empty_arguments_are_safe():
    assert to_engine_call("Z", None) == {"tool": "Z", "namespace": "", "args": {}}
    assert to_engine_call("Z", {}) == {"tool": "Z", "namespace": "", "args": {}}


def test_malformed_non_dict_arguments_dont_crash():
    # a malformed call still yields a scoreable dict (defensive)
    assert to_engine_call("W", "oops") == {"tool": "W", "namespace": "", "args": {}}


def test_empty_tool_name_passes_through():
    assert to_engine_call("", {})["tool"] == ""


def test_falsy_scope_value_is_ignored():
    # empty-string namespace shouldn't be chosen as a real scope
    out = to_engine_call("X", {"namespace": "", "scope": "ns/real"})
    assert out["namespace"] == "ns/real"


def test_output_round_trips_through_kagent_adapter():
    # the mapping's whole point: its output is what RuntimeAdapter.normalize already eats
    m = to_engine_call("DeleteNamespace", {"namespace": "checkout", "force": True})
    tc = KagentAdapter(task_type="t").observe(m)
    assert tc.tool == "DeleteNamespace"
    assert tc.scope == "checkout"
    assert tc.arguments == {"namespace": "checkout", "force": True}
