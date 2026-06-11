"""Reconcile an `AgenticArchitecture` into a declared contract + status (E11).

Pure, cluster-free, unit-testable — same split as reconcile.py for `AgentDriftPolicy`. The Kopf
handler in main.py calls `reconcile_architecture` and patches the CR status with the returned dict.
"""
from __future__ import annotations

from ..library.contract import DeclaredContract, build_contract


def reconcile_architecture(spec: dict) -> tuple[DeclaredContract, dict]:
    """Build the declared contract from an `AgenticArchitecture` spec and compute its status.

    Returns `(contract, status)`. The operator persists the contract where the engine reads policy
    (the operator-written store the proxy/interceptor mount read-only) and writes the status
    subresource. `status.contractHash` lets an `AgentDriftPolicy` pin a specific contract version.
    """
    contract = build_contract(spec)
    status = {
        "reconciled": True,
        "agents": len(contract.agents),
        "contractHash": contract.hash,
    }
    return contract, status
