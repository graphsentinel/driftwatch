"""Kopf control plane: watch AgentDriftPolicy, reconcile, write status, validate.

Kopf is an optional dependency (`pip install -e .[operator]`). The reconcile/validate
logic lives in reconcile.py / policy.py so it is testable without a cluster; these
handlers are thin wiring.
"""
from __future__ import annotations

from .policy import PolicyError, validate
from .reconcile import Reconciler

GROUP = "driftwatch.graphsentinel.org"
VERSION = "v1alpha1"
PLURAL = "agentdriftpolicies"

_reconcilers: dict[str, Reconciler] = {}


def _key(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


def run() -> None:  # console entry point: driftwatch-operator
    # Importing THIS module already registered the @kopf handlers below, so run the
    # embedded operator directly. (The old kopf.cli.main() raised AttributeError:
    # `import kopf` does not auto-import the kopf.cli submodule.) standalone=True keeps
    # the single-operator, no-peering posture the CLI `--standalone` flag intended.
    import kopf

    kopf.run(standalone=True)


try:  # register handlers only when kopf is available
    import os

    import kopf

    # Admission (validate) handler is OPT-IN. Registering a @kopf.on.validate handler
    # makes kopf require an admission server/tunnel; without one (the default — the
    # webhook is off in values.yaml, injector is roadmap) kopf aborts at startup with
    # "Admission handlers exist, but no admission server is configured". So only register
    # it when the chart enables the webhook (DRIFTWATCH_ADMISSION=1). Validation is NOT
    # lost when off: reconcile_policy below validates every create/update, and the CRD's
    # OpenAPI schema enforces structure at the API server.
    if os.environ.get("DRIFTWATCH_ADMISSION") == "1":  # pragma: no cover - needs webhook

        @kopf.on.validate(GROUP, VERSION, PLURAL)  # type: ignore[misc]
        def validate_policy(spec, name, **_):  # TC-F-01
            try:
                validate({**dict(spec), "_name": name})
            except PolicyError as e:
                raise kopf.AdmissionError(str(e), code=400)

    @kopf.on.create(GROUP, VERSION, PLURAL)  # type: ignore[misc]
    @kopf.on.update(GROUP, VERSION, PLURAL)  # type: ignore[misc]
    def reconcile_policy(spec, name, namespace, patch, **_):  # TC-F-02
        policy = validate({**dict(spec), "_name": name})
        rec = _reconcilers.get(_key(namespace, name)) or Reconciler(policy, persistent=True)
        _reconcilers[_key(namespace, name)] = rec
        patch.status.update(rec.status())

    @kopf.on.delete(GROUP, VERSION, PLURAL)  # type: ignore[misc]
    def forget_policy(name, namespace, **_):
        _reconcilers.pop(_key(namespace, name), None)

except ImportError:  # pragma: no cover - kopf not installed
    pass
