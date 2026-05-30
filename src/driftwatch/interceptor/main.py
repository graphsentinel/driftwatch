"""Interceptor console entry point (`driftwatch-interceptor`).

Matches the operator's main.py pattern and the CFP Repository Layout
(interceptor/main.py + interceptor/server.py). The actual ASGI app lives in
server.build_app; this wires a default Interceptor and serves it.
"""
from __future__ import annotations

import os

from ..adapters import KagentAdapter
from ..library.baseline import BaselineStore
from ..otel.emit import Emitter
from .engine import Interceptor
from .server import build_app


def build_default_interceptor() -> Interceptor:
    endpoint = os.environ.get("DRIFTWATCH_OTLP_ENDPOINT") or None
    return Interceptor(
        BaselineStore(),
        KagentAdapter(),
        emitter=Emitter(endpoint=endpoint),
    )


def run() -> None:  # pragma: no cover - console entry point
    import uvicorn

    uvicorn.run(build_app(build_default_interceptor()), host="0.0.0.0", port=8080)
