"""AgentDriftPolicy parsing + validation — pure logic, no Kubernetes import.

Kept separate from the Kopf handlers so it is unit-testable without a cluster
(TC-F-01 validation runs here; the webhook just calls validate()).
"""
from __future__ import annotations

from dataclasses import dataclass, field

VALID_ACTIONS = {"log", "drop", "block"}
VALID_FEATURES = {"tool", "scope", "sequence", "argSchemaHash"}
VALID_FAILURE = {"failClosed", "failOpen"}
VALID_ALGORITHMS = {"streaming-zscore"}


class PolicyError(ValueError):
    """Raised on an invalid AgentDriftPolicy spec (surfaced by the webhook)."""


@dataclass
class Policy:
    name: str
    selector: dict
    sources: list                      # str entries and/or {"models": [...]} dicts
    window: int
    features: list[str]
    threshold: float
    action: str
    failure_policy: str
    runtimes: list[dict] = field(default_factory=list)
    otel_endpoint: str | None = None
    emit_evaluation_on: list[str] = field(default_factory=list)

    @property
    def model_seed(self) -> list[str]:
        """Optional cold-start seed models, if a `models:` source is present (FR-9)."""
        for s in self.sources:
            if isinstance(s, dict) and "models" in s:
                return list(s["models"])
        return []


def validate(spec: dict) -> Policy:
    """Validate a spec dict and return a Policy, or raise PolicyError. This is TC-F-01."""
    if not isinstance(spec, dict):
        raise PolicyError("spec must be an object")

    action = spec.get("action")
    if action not in VALID_ACTIONS:
        raise PolicyError(f"action must be one of {sorted(VALID_ACTIONS)}, got {action!r}")

    baseline = spec.get("baseline") or {}
    sources = baseline.get("sources")
    if not sources or not isinstance(sources, list):
        raise PolicyError("baseline.sources must be a non-empty list")

    detection = spec.get("detection") or {}
    features = detection.get("features") or []
    bad = [f for f in features if f not in VALID_FEATURES]
    if bad:
        raise PolicyError(f"unknown detection.features {bad}; allowed {sorted(VALID_FEATURES)}")
    algo = detection.get("algorithm", "streaming-zscore")
    if algo not in VALID_ALGORITHMS:
        raise PolicyError(f"algorithm must be one of {sorted(VALID_ALGORITHMS)}")

    threshold = detection.get("threshold", 3.0)
    if not isinstance(threshold, (int, float)) or threshold <= 0:
        raise PolicyError("detection.threshold must be a positive number (raw z-score)")

    failure = spec.get("failurePolicy", "failClosed")
    if failure not in VALID_FAILURE:
        raise PolicyError(f"failurePolicy must be one of {sorted(VALID_FAILURE)}")

    for rt in spec.get("runtimes", []) or []:
        if "name" not in rt or "adapter" not in rt:
            raise PolicyError("each runtime needs name + adapter")

    otel = (spec.get("observability") or {}).get("otel") or {}
    return Policy(
        name=spec.get("_name", "agentdriftpolicy"),
        selector=spec.get("selector", {}),
        sources=sources,
        window=int(baseline.get("window", 50)),
        features=features or sorted(VALID_FEATURES),
        threshold=float(threshold),
        action=action,
        failure_policy=failure,
        runtimes=spec.get("runtimes", []) or [],
        otel_endpoint=otel.get("endpoint"),
        emit_evaluation_on=otel.get("emitEvaluationOn", []),
    )
