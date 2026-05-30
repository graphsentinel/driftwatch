"""FastAPI sidecar exposing the interceptor on the agent's tool path.

Thin shell over engine.Interceptor. FastAPI/uvicorn are optional (`.[interceptor]`).
Listens for MCP / OpenAI-tools / HTTP tool calls, returns 200 (forward/drop) or 403
(block) BEFORE the call reaches kube-apiserver.
"""
from __future__ import annotations

from .engine import Interceptor


def build_app(interceptor: Interceptor):  # pragma: no cover - needs fastapi
    from fastapi import FastAPI, Response

    app = FastAPI(title="driftwatch-interceptor")

    @app.post("/v1/tool-call")
    def tool_call(payload: dict, response: Response):
        verdict = interceptor.handle(payload)
        response.status_code = verdict.http_status
        return {
            "outcome": verdict.outcome,
            "gate_action": getattr(verdict.decision, "gate_action", None),
            "anomaly_kind": getattr(verdict.decision, "anomaly_kind", None),
            "signals": verdict.signals,
        }

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
