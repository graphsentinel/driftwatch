"""FastAPI sidecar exposing the interceptor on the agent's tool path.

Thin shell over engine.Interceptor. FastAPI/uvicorn are optional (`.[interceptor]`).
Listens for MCP / OpenAI-tools / HTTP tool calls, returns 200 (forward/drop) or 403
(block) BEFORE the call reaches kube-apiserver.
"""
from __future__ import annotations

from .engine import Interceptor


def apply_contract_push(interceptor: Interceptor, payload: dict) -> "tuple[int, dict]":
    """E13 §4e — apply one contract push to the interceptor's registry. Transport-free.

    Shared by the HTTP `/contracts` route (here) and the MCP proxy's custom route, so the
    multi-app store/route logic lives in one place. Body: {source, contract, ref?}.

    Multi-app (central DriftWatch, N AgentGates): each app pushes under its own `ref` (its app name);
    we store into `interceptor.contracts[ref]` — NOT a single field — so apps don't overwrite each
    other (consultant: the old `interceptor.contract = c` last-push-wins). A tool call's `_meta.app`
    then routes to the matching contract. The first push also seeds the metaless default. Returns
    `(http_status, body)`.
    """
    from ..library.contract import DeclaredContract, valid_ref
    try:
        c = DeclaredContract.from_dict(payload.get("contract") or {})
    except Exception as e:  # noqa: BLE001 — bad push → 400, never crash the proxy
        return 400, {"error": f"invalid contract: {e}"}
    ref = payload.get("ref") or payload.get("source") or "agentgate"
    if not valid_ref(ref):   # consultant MAJOR: ref → filename; reject path traversal / bad input
        return 400, {"error": f"invalid ref {ref!r}: must be alphanumeric + '_.-', start "
                              "alphanumeric, ≤64 chars (no path separators, no '..')"}
    if interceptor.contracts is None:
        interceptor.contracts = {}
    interceptor.contracts[ref] = c                 # registry: keyed by app, no cross-app overwrite
    if interceptor.contract is None:
        interceptor.contract = c                   # seed the metaless/single-app default (first push)
    try:  # best-effort persist (skipped if the data dir is read-only)
        import os

        from ..library.contract import save_contract
        save_contract(c, os.environ.get("DRIFTWATCH_DATA_DIR", "data"), ref)
    except Exception:  # noqa: BLE001
        pass
    return 200, {"stored": ref, "hash": c.hash, "source": payload.get("source"),
                 "apps": sorted(interceptor.contracts)}


def build_app(interceptor: Interceptor):  # pragma: no cover - needs fastapi
    from fastapi import Body, FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="driftwatch-interceptor")

    @app.post("/v1/tool-call")
    def tool_call(payload: dict = Body(...)):
        # NOTE: `response: Response` injection breaks under `from __future__ import
        # annotations` (FastAPI sees the stringized type as a query param), so set the
        # status via JSONResponse instead — robust regardless of annotation evaluation.
        verdict = interceptor.handle(payload)
        return JSONResponse(
            status_code=verdict.http_status,
            content={
                "outcome": verdict.outcome,
                "gate_action": getattr(verdict.decision, "gate_action", None),
                "anomaly_kind": getattr(verdict.decision, "anomaly_kind", None),
                "signals": verdict.signals,
            },
        )

    @app.post("/contracts")
    def register_contract(payload: dict = Body(...)):
        """E13 §4e — single-source interop. AgentGate (govern.proxyType=driftwatch) pushes its declared
        contract here; we store it (per-app `ref`) + hot-reload so the declared layer governs against it
        immediately — no separate `kubectl apply`. Multi-app: N apps coexist, routed by `_meta.app`."""
        status, body = apply_contract_push(interceptor, payload)
        return JSONResponse(status_code=status, content=body)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
