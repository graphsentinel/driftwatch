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
    import sys

    import kopf

    sys.argv = ["kopf", "run", "--standalone", __file__]
    kopf.cli.main()


try:  # register handlers only when kopf is available
    import kopf

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
