"""SQLite backend — the default persistent store.

Snapshots learned baselines (expected tools/scopes/arg-schemas, n-gram counts, rolling
numeric windows) as JSON blobs keyed by task type. Survives operator restarts; swap
for Postgres by reimplementing this one class.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from ..library.baseline import BaselineStore, TaskBaseline
from ..library.ngram import NGramModel
from ..library.zscore import StreamingZScore


def _default_db_path() -> str:
    # Base dir is configurable so the operator (non-root, read-only-ish workdir) writes
    # to a mounted writable volume. DRIFTWATCH_DATA_DIR is set by the Helm chart to an
    # emptyDir mount; falls back to a working-dir-relative "data/" for local/dev runs.
    base = os.environ.get("DRIFTWATCH_DATA_DIR", "data")
    return str(Path(base) / "baselines" / "driftwatch.db")


class SqliteBackend:
    def __init__(self, path: str | None = None):
        self.path = path or _default_db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_schema(self) -> None:
        with self._conn() as cx:
            cx.execute(
                "CREATE TABLE IF NOT EXISTS baselines "
                "(task_type TEXT PRIMARY KEY, window INTEGER, blob TEXT)"
            )

    def load(self, window: int) -> BaselineStore:
        store = BaselineStore(window=window)
        with self._conn() as cx:
            for task_type, blob in cx.execute("SELECT task_type, blob FROM baselines"):
                store._baselines[task_type] = _decode(json.loads(blob), window)
        return store

    def save(self, store: BaselineStore) -> None:
        with self._conn() as cx:
            for task_type, b in store._baselines.items():
                cx.execute(
                    "INSERT OR REPLACE INTO baselines (task_type, window, blob) VALUES (?,?,?)",
                    (task_type, b.window, json.dumps(_encode(b))),
                )


def _encode(b: TaskBaseline) -> dict:
    return {
        "task_type": b.task_type,
        "runs": b.runs,
        "expected_tools": sorted(b.expected_tools),
        "seen_scopes": sorted(b.seen_scopes),
        "seen_arg_schemas": {k: sorted(v) for k, v in b.seen_arg_schemas.items()},
        "ngram_counts": {",".join(k): v for k, v in b.sequence._counts.items()},
        "ngram_total": b.sequence._total,
        "chain_length": list(b.chain_length._values),
        "max_risk": list(b.max_risk._values),
    }


def _decode(d: dict, window: int) -> TaskBaseline:
    b = TaskBaseline(task_type=d["task_type"], window=window)
    b.runs = d["runs"]
    b.expected_tools = set(d["expected_tools"])
    b.seen_scopes = set(d["seen_scopes"])
    b.seen_arg_schemas = {k: set(v) for k, v in d["seen_arg_schemas"].items()}
    ng = NGramModel()
    for k, v in d["ngram_counts"].items():
        ng._counts[tuple(k.split(","))] = v
    ng._total = d["ngram_total"]
    b.sequence = ng
    cl = StreamingZScore(window)
    for v in d["chain_length"]:
        cl.update(v)
    b.chain_length = cl
    mr = StreamingZScore(window)
    for v in d["max_risk"]:
        mr.update(v)
    b.max_risk = mr
    return b
