"""DriftWatch interceptor (data plane) — enforcement engine + FastAPI sidecar."""
from .engine import BLOCK, DROP, FORWARD, Interceptor, Verdict
from .main import build_default_interceptor, run

__all__ = ["Interceptor", "Verdict", "FORWARD", "DROP", "BLOCK",
           "run", "build_default_interceptor"]
