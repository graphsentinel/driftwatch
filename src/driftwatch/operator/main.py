"""Kopf control plane: watch AgentDriftPolicy, reconcile, write status, validate.

Kopf is an optional dependency (`pip install -e .[operator]`). The reconcile/validate
logic lives in reconcile.py / policy.py so it is testable without a cluster; these
handlers are thin wiring.
"""
from __future__ import annotations

from .architecture import reconcile_architecture
from .policy import PolicyError, validate
from .reconcile import Reconciler

GROUP = "driftwatch.graphsentinel.org"
VERSION = "v1alpha1"
PLURAL = "agentdriftpolicies"
PLURAL_ARCH = "agenticarchitectures"   # E11: the configure/declare CRD

_reconcilers: dict[str, Reconciler] = {}
_contracts: dict[str, object] = {}     # E11: name -> DeclaredContract (engine reads these)


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

    # E11 — AgenticArchitecture (configure/declare): reconcile the org into a declared contract
    # the engine scores against. Same thin-wiring pattern as the policy handlers above.
    @kopf.on.create(GROUP, VERSION, PLURAL_ARCH)  # type: ignore[misc]
    @kopf.on.update(GROUP, VERSION, PLURAL_ARCH)  # type: ignore[misc]
    def reconcile_arch(spec, name, namespace, patch, **_):
        from ..library.contract import save_contract

        contract, status = reconcile_architecture(dict(spec))
        _contracts[_key(namespace, name)] = contract
        # Persist to the data plane (DRIFTWATCH_DATA_DIR, the same writable volume the baseline
        # uses) so the interceptor/proxy can load it read-only by name (AgentDriftPolicy.contractRef).
        save_contract(contract, os.environ.get("DRIFTWATCH_DATA_DIR", "data"), name)
        patch.status.update(status)

    @kopf.on.delete(GROUP, VERSION, PLURAL_ARCH)  # type: ignore[misc]
    def forget_arch(name, namespace, **_):
        from ..library.contract import delete_contract

        _contracts.pop(_key(namespace, name), None)
        delete_contract(os.environ.get("DRIFTWATCH_DATA_DIR", "data"), name)

except ImportError:  # pragma: no cover - kopf not installed
    pass
