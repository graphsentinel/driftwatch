"""Enforcement engine: score a live call and apply log / drop / block.

Pure, transport-free — the FastAPI server (server.py) is a thin shell over this so it
is unit-testable (TC-F-05/06/07/11). The score happens on the agent's decision, not on
the resulting API object: the call is evaluated BEFORE it reaches kube-apiserver.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..library.baseline import BaselineStore
from ..library.decision import score_chain
from ..otel.emit import Emitter
from ..sdk.observation import DecisionChain, RuntimeAdapter, ToolCall

if TYPE_CHECKING:
    from ..library.contract import DeclaredContract

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
        features: "set[str] | None" = None,
        emitter: Emitter | None = None,
        contract: "DeclaredContract | None" = None,
        cc_model: str = "", cc_endpoint: str = "", cc_provider: str = "ollama",
        cc_votes: int = 1, cc_timeout: float = 90.0,
    ):
        self.store = store
        self.adapter = adapter
        self.threshold = threshold
        self.action = action
        self.failure_policy = failure_policy
        self.features = features            # None -> all four (FR-2 detection.features)
        self.emitter = emitter or Emitter()
        self.contract = contract            # E11: declared contract (None -> standalone, E1–E10)
        # E13 §4c — prompt-aware cross-check (shadow): light model + N-vote, off unless enabled.
        self.cc_model = cc_model
        self.cc_endpoint = cc_endpoint
        self.cc_provider = cc_provider
        self.cc_votes = cc_votes
        self.cc_timeout = cc_timeout

    def handle(self, raw_call: dict, baseline_id: str = "baseline.v1") -> Verdict:
        """Normalize -> [declared check] -> score -> enforce. Never raises; fails per policy."""
        try:
            call: ToolCall = self.adapter.observe(raw_call)
            chain: DecisionChain = self.adapter.chain

            # E11 declared check (configure/declare layer): a deterministic known-bad pre-check
            # against the declared contract, BEFORE the statistical baseline. A call to a tool the
            # agent is not bound to, or out of its declared scope, is a *declared violation* and is
            # blocked regardless of baseline readiness. No contract -> skipped (standalone == E1–E10).
            if self.contract is not None:
                risk = self.contract.risk_map.get(call.tool, call.risk)
                # E11 single-call check (unbound tool / out-of-scope) — runs first.
                reason = self.contract.check(chain.agent_id, call.tool, call.scope)
                if reason is not None:
                    signals = self.emitter.emit_declared(
                        chain, call.tool, reason, category=call.category, risk=risk)
                    return Verdict(BLOCK, 403, None, signals)
                # E12 declared chain-rule (forbidden deny-sequence on the chain's tail).
                sreason = self.contract.check_sequence(chain.agent_id, chain.tools)
                if sreason is not None:
                    signals = self.emitter.emit_declared(
                        chain, call.tool, sreason, category=call.category, risk=risk,
                        kind="declared_sequence")
                    return Verdict(BLOCK, 403, None, signals)

            baseline = self.store.get(chain.task_type)
            if not baseline.ready:
                return self._cold_start()  # TC-F-04 at runtime (no cross-check on the block path)

            decision = score_chain(chain, baseline, threshold=self.threshold,
                                   action=self.action, features=self.features)
            signals = self.emitter.emit(chain, decision, baseline_id)

            if not decision.is_drift or decision.gate_action == "log":
                # E13 §4c shadow cross-check — only on the FORWARD path (consultant review): it is a
                # learned signal, not enforcement, so it must not add latency to a deny/cold-start
                # decision. Emit-only, bounded by a total deadline; never changes the verdict.
                self._maybe_cross_check(raw_call, call, chain)
                return Verdict(FORWARD, 200, decision, signals)   # within baseline / shadow
            if decision.gate_action == "drop":
                return Verdict(DROP, 200, decision, signals)      # silent no-op
            return Verdict(BLOCK, 403, decision, signals)         # block

        except Exception:  # interceptor failure -> declared posture (TC-F-11)
            return self._fail()

    def _maybe_cross_check(self, raw_call: dict, call: "ToolCall", chain: "DecisionChain") -> None:
        """E13 §4c — prompt-aware second opinion (SHADOW): emit-only, never changes the verdict.

        Off unless DRIFTWATCH_CROSS_CHECK_ENABLED. Needs the prompt (from the agent's MCP `_meta`) and
        candidate tools (meta `tools`, else the chain's). A light LLM predicts the expected tool; if
        the agent's actual tool diverges, emit a `danger_detected` signal. Any failure → no signal
        (hard blocks stay on the declared layer; this is a learned, additive flag).
        """
        import logging
        log = logging.getLogger("driftwatch.cross_check")
        from .cross_check import cross_check, cross_check_enabled
        if not cross_check_enabled():
            return
        meta = raw_call.get("meta") or {}
        prompt = meta.get("prompt")
        candidates = tuple(meta.get("tools") or chain.tools)
        if not prompt or not candidates:
            log.warning("cross-check SKIP tool=%s has_prompt=%s candidates=%d (meta_keys=%s)",
                        call.tool, bool(prompt), len(candidates), sorted(meta.keys()))
            return
        try:
            r = cross_check(prompt, call.tool, candidates, model=self.cc_model,
                            endpoint=self.cc_endpoint, provider=self.cc_provider,
                            votes=self.cc_votes, total_timeout=self.cc_timeout)
        except Exception:  # noqa: BLE001 — shadow must never break the hop
            return
        import logging
        logging.getLogger("driftwatch.cross_check").warning(
            "cross-check tool=%s predicted=%s diverged=%s votes=%d conf=%.2f",
            call.tool, r.predicted, r.diverged, r.votes, r.confidence)
        if r.diverged:
            self.emitter.emit_cross_check(chain, call.tool, predicted=r.predicted,
                                          confidence=r.confidence)

    def _cold_start(self) -> Verdict:
        if self.failure_policy == "failClosed":
            return Verdict(BLOCK, 403, None, {"reason": "cold-start failClosed"})
        return Verdict(FORWARD, 200, None, {"reason": "cold-start failOpen"})

    def _fail(self) -> Verdict:
        if self.failure_policy == "failClosed":
            return Verdict(BLOCK, 403, None, {"reason": "interceptor error failClosed"})
        return Verdict(FORWARD, 200, None, {"reason": "interceptor error failOpen"})
