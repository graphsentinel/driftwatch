"""E13 §4c — LLM cross-check detector (prompt-aware second opinion).

The agent SELECTS the tool with its own LLM; DriftWatch, given the SAME prompt, has a *light* LLM
PREDICT the expected tool. The divergence between selection and prediction is a drift signal —
prompt-aware, so it catches what a per-task_type baseline cannot (a prompt-driven chain shape).

This module is deliberately **pure and standalone** (one `httpx` call, no proxy/engine coupling) so it
unit-tests without a live MCP hop. Soundness per §4c: a *light* model, **N-vote**, **shadow** (this
module only computes a signal — emission/blocking is the caller's choice; hard blocks stay declared),
and graceful (no prompt / LLM down / unparseable → no signal, never raise).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CrossCheckResult:
    diverged: bool                 # observed tool not among the predicted set (the drift signal)
    predicted: tuple[str, ...]     # the predicted tool(s) (majority across votes)
    observed: str
    votes: int                     # how many predictions actually returned a usable answer
    confidence: float              # fraction of votes that agreed on the winning prediction


def _env(key: str, default: str = "") -> str:
    """Read DRIFTWATCH_<key> then AGENTGATE_<key> then `default` (mirror of the runtime helper)."""
    return os.environ.get(f"DRIFTWATCH_{key}") or os.environ.get(f"AGENTGATE_{key}") or default


def _match(predicted: str, candidates: tuple[str, ...]) -> str:
    """Map a free-form model answer to a candidate tool name (exact, else substring), else ''."""
    p = predicted.strip().strip("`\"'").lower()
    for c in candidates:
        if c.lower() == p:
            return c
    for c in candidates:                       # the model often answers with extra words
        if c.lower() in p or p in c.lower():
            return c
    return ""


def predict_expected_tool(prompt: str, candidates: tuple[str, ...] | list[str], *,
                          model: str, endpoint: str = "", provider: str = "ollama", nonce: int = 0,
                          timeout: float = 90.0) -> str | None:
    """One light-LLM call: given `prompt`, which of `candidates` would you call? → a candidate or None.

    `provider`: `ollama` (native /api/chat) OR an openai-compatible endpoint (openai/azure/runpod/
    vllm — /v1/chat/completions, `endpoint`=base_url, key from <PROVIDER>_API_KEY). Returns None on
    any failure (unreachable, bad response, no match) — a missing prediction is "no signal", never an
    exception. `nonce` varies the call across votes.
    """
    import os
    cands = tuple(candidates)
    if not prompt or not cands:
        return None
    provider = (provider or "ollama").lower()
    sys_msg = ("You verify tool selection. Given the user's goal and a list of available tools, reply "
               "with EXACTLY ONE tool name from the list that should be called — no other words.")
    user = (f"Goal: {prompt}\nAvailable tools: {', '.join(cands)}\n"
            f"Answer with one tool name from the list.{'' if not nonce else f' (#{nonce})'}")
    messages = [{"role": "system", "content": sys_msg}, {"role": "user", "content": user}]
    try:
        import httpx
        if provider == "ollama":
            host = (endpoint or _env("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
            resp = httpx.post(f"{host}/api/chat",
                              json={"model": model, "messages": messages, "stream": False},
                              timeout=timeout)
            resp.raise_for_status()
            answer = (resp.json().get("message", {}) or {}).get("content", "") or ""
        elif provider == "anthropic":
            base = (endpoint or "https://api.anthropic.com").rstrip("/")
            headers = {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                       "anthropic-version": "2023-06-01", "content-type": "application/json"}
            resp = httpx.post(f"{base}/v1/messages",
                              json={"model": model, "max_tokens": 64, "system": sys_msg,
                                    "messages": [{"role": "user", "content": user}]},
                              headers=headers, timeout=timeout)
            resp.raise_for_status()
            content = resp.json().get("content") or []
            answer = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        else:  # openai-compatible (openai/azure/runpod/vllm/tgi)
            base = (endpoint or "https://api.openai.com/v1").rstrip("/")
            key = (os.environ.get(f"{provider.upper().replace('-', '_')}_API_KEY")
                   or os.environ.get("OPENAI_API_KEY", ""))
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            resp = httpx.post(f"{base}/chat/completions",
                              json={"model": model, "messages": messages, "stream": False},
                              headers=headers, timeout=timeout)
            resp.raise_for_status()
            answer = (((resp.json().get("choices") or [{}])[0] or {}).get("message", {})
                      or {}).get("content", "") or ""
    except Exception:  # noqa: BLE001 — LLM down / bad response → no signal
        return None
    return _match(answer, cands) or None


def cross_check(prompt: str, observed_tool: str, candidates: tuple[str, ...] | list[str], *,
                model: str, endpoint: str = "", provider: str = "ollama", votes: int = 1,
                total_timeout: float = 90.0) -> CrossCheckResult:
    """Predict up to `votes` times, majority-vote, and flag divergence (observed not in predicted).

    `total_timeout` is a HARD wall across ALL votes (consultant review): votes run sequentially, each
    given only the remaining budget, so the detector can never add more than `total_timeout` to the
    tool path regardless of `votes`. Divergence is reported only when ≥1 usable prediction came back
    AND it disagrees with the observed tool — an all-empty panel yields `diverged=False` (no signal).
    """
    import time
    cands = tuple(candidates)
    deadline = time.monotonic() + max(1.0, total_timeout)
    preds: list[str] = []
    for i in range(max(1, votes)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break                                   # budget spent — stop voting (no signal beats hang)
        p = predict_expected_tool(prompt, cands, model=model, endpoint=endpoint, provider=provider,
                                  nonce=i,
                                  timeout=remaining)
        if p:
            preds.append(p)

    if not preds:                              # nothing came back → no signal
        return CrossCheckResult(False, (), observed_tool, 0, 0.0)

    # majority winner across the votes that answered
    counts: dict[str, int] = {}
    for p in preds:
        counts[p] = counts.get(p, 0) + 1
    top = max(counts.values())
    winners = tuple(sorted(t for t, n in counts.items() if n == top))
    confidence = top / len(preds)
    diverged = observed_tool not in winners
    return CrossCheckResult(diverged, winners, observed_tool, len(preds), confidence)


def cross_check_enabled() -> bool:
    """Shadow detection is off by default — opt in with DRIFTWATCH_CROSS_CHECK_ENABLED=true."""
    return _env("CROSS_CHECK_ENABLED", "false").strip().lower() == "true"
