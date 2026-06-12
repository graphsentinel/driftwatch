"""FastAPI sidecar exposing the interceptor on the agent's tool path.

Thin shell over engine.Interceptor. FastAPI/uvicorn are optional (`.[interceptor]`).
Listens for MCP / OpenAI-tools / HTTP tool calls, returns 200 (forward/drop) or 403
(block) BEFORE the call reaches kube-apiserver.
"""
from __future__ import annotations

from .engine import Interceptor


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
        contract here; we store + hot-reload it so the declared layer governs against it immediately —
        no separate `kubectl apply` of an AgenticArchitecture. Body: {source, contract, ref?}."""
        from ..library.contract import DeclaredContract
        try:
            c = DeclaredContract.from_dict(payload.get("contract") or {})
        except Exception as e:  # noqa: BLE001 — bad push → 400, never crash the proxy
            return JSONResponse(status_code=400, content={"error": f"invalid contract: {e}"})
        interceptor.contract = c                      # hot-reload: govern against it now
        ref = payload.get("ref") or "agentgate"
        try:  # best-effort persist (skipped if the data dir is read-only)
            import os

            from ..library.contract import save_contract
            save_contract(c, os.environ.get("DRIFTWATCH_DATA_DIR", "data"), ref)
        except Exception:  # noqa: BLE001
            pass
        return {"stored": ref, "hash": c.hash, "source": payload.get("source")}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
