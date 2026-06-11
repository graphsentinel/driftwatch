"""S4 tests — the five demo scenarios reproduce with the right anomaly kind + action."""
import pytest

from driftwatch.cli import SCENARIOS, run_demo


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_demo_scenario_passes(scenario):
    # run_demo returns 0 only when anomaly_kind + gate_action match the expectation
    assert run_demo(scenario) == 0


def test_unknown_scenario_errors():
    assert run_demo("nope") == 2


def test_all_five_present():
    assert set(SCENARIOS) == {
        "tool_substitution", "scope_escalation", "sequence_inversion",
        "argument_injection", "retry_storm",
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
