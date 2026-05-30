"""Enforcement engine: score a live call and apply log / drop / block.

Pure, transport-free — the FastAPI server (server.py) is a thin shell over this so it
is unit-testable (TC-F-05/06/07/11). The score happens on the agent's decision, not on
the resulting API object: the call is evaluated BEFORE it reaches kube-apiserver.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..library.baseline import BaselineStore
from ..library.decision import score_chain
from ..otel.emit import Emitter
from ..sdk.observation import DecisionChain, RuntimeAdapter, ToolCall

FORWARD = "forward"   # within baseline (or log) -> goes to the API
DROP = "drop"         # silent no-op
BLOCK = "block"       # 403 to the agent


@dataclass
class Verdict:
    outcome: str          # forward | drop | block
    http_status: int      # 200 forward, 200 drop (empty), 403 block
    decision: object      # the library.Decision (or None on cold-start/failure)
    signals: dict         # {"span":..., "event":...} or {"reason":...}


class Interceptor:
    """One interceptor per governed pod. Holds the baseline + policy knobs."""

    def __init__(
        self,
        store: BaselineStore,
        adapter: RuntimeAdapter,
        *,
        threshold: float = 3.0,
        action: str = "block",
        failure_policy: str = "failClosed",
        emitter: Emitter | None = None,
    ):
        self.store = store
        self.adapter = adapter
        self.threshold = threshold
        self.action = action
        self.failure_policy = failure_policy
        self.emitter = emitter or Emitter()

    def handle(self, raw_call: dict, baseline_id: str = "baseline.v1") -> Verdict:
        """Normalize -> score -> enforce. Never raises to the caller; fails per policy."""
        try:
            call: ToolCall = self.adapter.observe(raw_call)  # noqa: F841 (appends to chain)
            chain: DecisionChain = self.adapter.chain
            baseline = self.store.get(chain.task_type)

            if not baseline.ready:
                return self._cold_start()  # TC-F-04 at runtime

            decision = score_chain(chain, baseline, threshold=self.threshold, action=self.action)
            signals = self.emitter.emit(chain, decision, baseline_id)

            if not decision.is_drift or decision.gate_action == "log":
                return Verdict(FORWARD, 200, decision, signals)   # within baseline / shadow
            if decision.gate_action == "drop":
                return Verdict(DROP, 200, decision, signals)      # silent no-op
            return Verdict(BLOCK, 403, decision, signals)         # block

        except Exception:  # interceptor failure -> declared posture (TC-F-11)
            return self._fail()

    def _cold_start(self) -> Verdict:
        if self.failure_policy == "failClosed":
            return Verdict(BLOCK, 403, None, {"reason": "cold-start failClosed"})
        return Verdict(FORWARD, 200, None, {"reason": "cold-start failOpen"})

    def _fail(self) -> Verdict:
        if self.failure_policy == "failClosed":
            return Verdict(BLOCK, 403, None, {"reason": "interceptor error failClosed"})
        return Verdict(FORWARD, 200, None, {"reason": "interceptor error failOpen"})
