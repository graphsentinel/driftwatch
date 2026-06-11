"""Baseline persistence — swappable backends (memory for dev, sqlite by default)."""
from .memory import MemoryBackend
from .sqlite import SqliteBackend
from .store import BaselineBackend

__all__ = ["BaselineBackend", "MemoryBackend", "SqliteBackend"]
