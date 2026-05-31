"""Interceptor console entry point (`driftwatch-interceptor`).

Matches the operator's main.py pattern and the CFP Repository Layout
(interceptor/main.py + interceptor/server.py). The actual ASGI app lives in
server.build_app; this wires a default Interceptor and serves it.

Control->data-plane handoff (FR-10): the sidecar does NOT start empty. It loads the
operator-reconciled baseline from the shared store (DRIFTWATCH_DATA_DIR) and takes the
policy knobs (action / threshold / failurePolicy / features) the operator wrote into the
environment, so it enforces the SAME policy the operator reconciled — not hard-coded
defaults.
"""
from __future__ import annotations

import os

from ..adapters import KagentAdapter
from ..db import MemoryBackend, SqliteBackend
from ..library.baseline import BaselineStore
from ..otel.emit import Emitter
from .engine import Interceptor
from .server import build_app


def _load_baseline(window: int) -> BaselineStore:
    """Load the operator-written baseline snapshot, or an empty store on cold start.

    With DRIFTWATCH_DATA_DIR set (the chart mounts it) the sqlite snapshot the operator
    persists is read; without it (dev/tests) fall back to an empty in-memory store.
    """
    if os.environ.get("DRIFTWATCH_DATA_DIR"):
        try:
            # read_only: the sidecar mounts the operator's baseline read-only and must not
            # write (no mkdir / CREATE TABLE) — that would fail on a readOnly mount. A
            # missing db (operator hasn't seeded yet) raises here -> safe cold-start below.
            return SqliteBackend(read_only=True).load(window=window)
        except Exception:
            return MemoryBackend().load(window=window)
    return MemoryBackend().load(window=window)


def _policy_features() -> "set[str] | None":
    raw = os.environ.get("DRIFTWATCH_FEATURES", "").strip()
    return {f.strip() for f in raw.split(",") if f.strip()} or None


def build_default_interceptor() -> Interceptor:
    # Policy knobs delivered by the operator via the pod env (FR-10).
    endpoint = os.environ.get("DRIFTWATCH_OTLP_ENDPOINT") or None
    action = os.environ.get("DRIFTWATCH_ACTION", "block")
    failure_policy = os.environ.get("DRIFTWATCH_FAILURE_POLICY", "failClosed")
    try:
        threshold = float(os.environ.get("DRIFTWATCH_THRESHOLD", "3.0"))
    except ValueError:
        threshold = 3.0
    try:
        window = int(os.environ.get("DRIFTWATCH_WINDOW", "50"))
    except ValueError:
        window = 50

    return Interceptor(
        _load_baseline(window),
        KagentAdapter(),
        threshold=threshold,
        action=action,
        failure_policy=failure_policy,
        features=_policy_features(),
        emitter=Emitter(endpoint=endpoint),
    )


def run() -> None:  # pragma: no cover - console entry point
    import uvicorn

    uvicorn.run(build_app(build_default_interceptor()), host="0.0.0.0", port=8080)
