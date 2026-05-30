# DEMO RUNBOOK — DriftWatch (35-min session)

Stage-time choreography. Goal: show the **CRD**, catch **decision drift** across five
scenarios, on **Kagent + Goose**, live on Grafana.

## Pre-flight (before you walk on)
- [ ] `make obs-up` — confirm Grafana (`:3000`) + Jaeger (`:16686`) are green.
- [ ] `make cluster-up && make deploy` then `kubectl apply -f manifests/sample-agents.yaml` — `kubectl get adp` ready after the shadow policy seeds.
- [ ] OTLP reachability: a quick `make demo-1` in-cluster shows a span in Jaeger.
- [ ] Terminal font huge; Grafana dashboard pre-opened on *agent-decisions*.
- [ ] `recordings/` casts ready as fallback.

## Beat sheet
1. **00:00 Hook** — the 3 a.m. fear: an agent that does the wrong right-thing. Show the CRD (`kubectl get adp demo-shadow -o yaml`) — "this is what the agent is *allowed to mean*."
2. **03:00 Where it sits** — admission sees a valid request; DriftWatch sees the *decision*. One diagram.
3. **08:00 Shadow mode** — `kubectl apply -f manifests/agentdriftpolicy-shadow.yaml` (`action: log`); run normal traffic; Grafana shows scores, nothing blocked. Trust-building (NFR-5).
4. **12:00 Flip to block** — `kubectl apply -f manifests/agentdriftpolicy-enforce.yaml`.
5. **14:00 Five scenarios** — `make demo-1..5`. For each: show the chain, the `gate.action`, the `gen_ai.agent.computed.anomaly.kind`, and the Grafana panel update.
   - demo-1 tool substitution → block (Kagent)
   - demo-2 scope escalation → block
   - demo-3 sequence inversion → drop
   - demo-4 argument injection → block
   - demo-5 retry storm → drop (Goose)
6. **24:00 The one we got wrong** — show a false positive; tune `threshold`/`window`; it stops blocking. FP rate panel drops.
7. **27:00 Inverse scaling** — `make eval`: bigger model didn't drift less (β₁ > 0).
8. **30:00 Takeaways** — operator + Helm + CRD + the `gen_ai.agent.*` schema; Apache-2.0.
9. **32:00 Q&A**

## If the cluster dies
Switch to `recordings/sN.cast` (asciinema) for the affected scenario; keep narrating.
The standalone `make demo-N` also works with only Python — no cluster, no compose.
