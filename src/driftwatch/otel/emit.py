"""Emit a scored Decision as the gen_ai.agent.* schema: a span + an evaluation event.

Degrades gracefully when opentelemetry isn't installed (returns the attribute dicts so
tests and the interceptor can assert on them without a live collector).
"""
from __future__ import annotations

from ..library.decision import Decision
from ..sdk.observation import DecisionChain
from . import attributes as A


def build_span_attributes(chain: DecisionChain, decision: Decision, baseline_id: str) -> dict:
    """The span attributes for one scored decision (Constraints C1)."""
    attrs: dict[str, object] = {
        A.GEN_AI_AGENT_ID: chain.agent_id,
        A.GEN_AI_AGENT_TASK_TYPE: chain.task_type,
        A.GEN_AI_AGENT_BASELINE_ID: baseline_id,
        A.GEN_AI_AGENT_BASELINE_EXPECTED_TOOLS: decision.expected_tools,
        A.GEN_AI_TOOL_NAME: decision.observed_tool,
        A.GEN_AI_AGENT_TOOL_CATEGORY: decision.observed_category,
        A.GEN_AI_AGENT_TOOL_PARAMETERS_HASH: decision.observed_arg_hash,
        A.GEN_AI_AGENT_TOOL_RISK_SEVERITY: decision.observed_risk,
        A.GEN_AI_AGENT_BASELINE_MATCH: decision.baseline_match,
        A.GEN_AI_AGENT_GATE_ACTION: decision.gate_action,
        A.GEN_AI_AGENT_GATE_BLOCKED: decision.gate_blocked,
        A.GEN_AI_AGENT_GATE_REASON: decision.reason,
    }
    if decision.is_drift:
        attrs[A.GEN_AI_AGENT_COMPUTED_ANOMALY] = True
        if decision.anomaly_kind:
            attrs[A.GEN_AI_AGENT_COMPUTED_ANOMALY_KIND] = decision.anomaly_kind
        if decision.feature == "scope":
            attrs[A.GEN_AI_AGENT_SCOPE_ESCALATION_ATTEMPTED] = True
    return attrs


def build_evaluation_event(decision: Decision) -> dict:
    """The gen_ai.evaluation.result event payload — the score lives here, not on the span."""
    return {
        A.GEN_AI_EVALUATION_NAME: A.EVAL_BASELINE_DEVIATION,
        A.GEN_AI_EVALUATION_SCORE_VALUE: round(decision.score_value, 4),
        A.GEN_AI_EVALUATION_SCORE_LABEL: decision.score_label,
        A.GEN_AI_EVALUATION_EXPLANATION: decision.reason,
    }


class Emitter:
    """Pushes the schema to an OTLP endpoint, or no-ops if OTel isn't available."""

    def __init__(self, service_name: str = "driftwatch", endpoint: str | None = None):
        self.service_name = service_name
        self.endpoint = endpoint
        self._tracer = None
        self._m_decisions = None
        self._m_anomaly = None
        self._m_score = None
        # Only wire a live OTLP exporter when an endpoint is explicitly configured.
        # With no endpoint (tests, demos, offline scoring) we stay a pure dict builder —
        # no background export thread, no global-provider side effects.
        if not endpoint:
            return
        try:  # optional dependency — interceptor extra
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.metrics.view import (
                ExplicitBucketHistogramAggregation,
                View,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            # The collector listens on plaintext gRPC; the OTLP exporter defaults to TLS
            # (a bare host:port then fails with SSL WRONG_VERSION_NUMBER). Use insecure
            # unless the endpoint explicitly opts into TLS via an https:// scheme.
            insecure = not endpoint.startswith("https://")
            resource = Resource.create({"service.name": service_name})

            provider = TracerProvider(resource=resource)
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
            )
            trace.set_tracer_provider(provider)
            # instrumentation scope follows the service name (driftwatch default, agentgate for E13)
            self._tracer = trace.get_tracer(service_name)

            # Metrics power the Grafana dashboard (Prometheus scrapes the collector's
            # :8889). They surface in Prometheus as driftwatch_decisions_total /
            # driftwatch_anomaly_total / driftwatch_score_value_bucket — the names the
            # dashboard panels query.
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=insecure),
                export_interval_millis=5000,
            )
            # The score is normalized to [0,1]; the default histogram buckets (5,10,25…)
            # would dump every value into the first bucket. Use explicit [0,1] boundaries
            # so the dashboard's quantiles are meaningful (R10a).
            # metric names follow the service name (driftwatch_* default, agentgate_* for E13) so the
            # two products surface as distinct Prometheus series.
            score_view = View(
                instrument_name=f"{service_name}.score.value",
                aggregation=ExplicitBucketHistogramAggregation(
                    [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
                ),
            )
            metrics.set_meter_provider(
                MeterProvider(resource=resource, metric_readers=[reader], views=[score_view])
            )
            meter = metrics.get_meter(service_name)
            self._m_decisions = meter.create_counter(
                f"{service_name}.decisions", description="scored decisions by gate.action")
            self._m_anomaly = meter.create_counter(
                f"{service_name}.anomaly", description="drift decisions by computed.anomaly.kind")
            self._m_score = meter.create_histogram(
                f"{service_name}.score.value", description="normalized drift score [0,1]")
        except Exception:
            self._tracer = None  # graceful: build_* still usable for tests/logs

    def emit(self, chain: DecisionChain, decision: Decision, baseline_id: str) -> dict:
        span_attrs = build_span_attributes(chain, decision, baseline_id)
        event_attrs = build_evaluation_event(decision)
        if self._tracer is not None:  # pragma: no cover - needs OTel + collector
            with self._tracer.start_as_current_span(
                A.execute_tool_span(decision.observed_tool)
            ) as span:
                for k, v in span_attrs.items():
                    span.set_attribute(k, v)
                span.add_event(A.GEN_AI_EVALUATION_RESULT_EVENT, attributes=event_attrs)
        if self._m_decisions is not None:  # pragma: no cover - needs OTel + collector
            self._m_decisions.add(1, {"gate_action": decision.gate_action})
            self._m_score.record(decision.score_value, {"gate_action": decision.gate_action})
            if decision.is_drift and decision.anomaly_kind:
                self._m_anomaly.add(1, {"anomaly_kind": decision.anomaly_kind})
        return {"span": span_attrs, "event": event_attrs}

    def emit_agent_run(self, *, agent_id: str, task_type: str, model: str = "",
                       tools: list[str] | tuple[str, ...] = (),
                       tool_calls: list[dict] | tuple[dict, ...] = (),
                       violation: dict | None = None,
                       attributes: tuple[str, ...] | list[str] | None = None) -> dict:
        """Emit one AgentGate agent run as a `gen_ai.agent.*` span (E13 observability, §4b).

        Same schema as the drift core (C1: no `drift.*`). Carries the agent identity, model, bound
        tools, the per-run tool-call trace (as span events), and — for a dynamic delegation gate — a
        declared violation (`gate.declared=true`, `anomaly.kind=delegation_violation`). No endpoint →
        pure dict builder (returns the attrs for tests, emits nothing).

        `attributes` is the CRD-configurable allow-list: `None`/`()`/`["*"]` → all; `["none"]` →
        emit nothing (no span); else only the listed attribute keys. C1-safe — only ever a subset.
        """
        allow = tuple(attributes or ())
        if "none" in allow:
            return {"span": {}, "emitted": False}        # telemetry off for this org
        allow_all = (not allow) or ("*" in allow)

        span_attrs: dict[str, object] = {
            A.GEN_AI_AGENT_ID: agent_id,
            A.GEN_AI_AGENT_TASK_TYPE: task_type,
        }
        if model:
            span_attrs[A.GEN_AI_REQUEST_MODEL] = model
        if tools:
            span_attrs[A.GEN_AI_AGENT_TOOLS] = list(tools)
        if violation:
            span_attrs[A.GEN_AI_AGENT_GATE_DECLARED] = True
            span_attrs[A.GEN_AI_AGENT_GATE_BLOCKED] = True
            span_attrs[A.GEN_AI_AGENT_GATE_REASON] = violation.get("reason", "")
            span_attrs[A.GEN_AI_AGENT_COMPUTED_ANOMALY] = True
            span_attrs[A.GEN_AI_AGENT_COMPUTED_ANOMALY_KIND] = "delegation_violation"
        if not allow_all:
            span_attrs = {k: v for k, v in span_attrs.items() if k in allow}
        emit_tool_events = allow_all or (A.GEN_AI_AGENT_TOOL_CALL_EVENT in allow)

        if self._tracer is not None:  # pragma: no cover - needs OTel + collector
            with self._tracer.start_as_current_span(f"agent.run {agent_id}") as span:
                for k, v in span_attrs.items():
                    span.set_attribute(k, v)
                if emit_tool_events:
                    for tc in tool_calls:
                        span.add_event(A.GEN_AI_AGENT_TOOL_CALL_EVENT,
                                       attributes={A.GEN_AI_TOOL_NAME: tc.get("tool", ""),
                                                   A.GEN_AI_AGENT_TOOL_ALLOWED: bool(tc.get("ok"))})
        if self._m_decisions is not None:  # pragma: no cover - needs OTel + collector
            self._m_decisions.add(1, {"gate_action": "block" if violation else "log"})
            if violation:
                self._m_anomaly.add(1, {"anomaly_kind": "delegation_violation"})
        return {"span": span_attrs, "emitted": True}

    def emit_declared(self, chain: DecisionChain, tool: str, reason: str,
                      *, category: str = "", risk: int = 0,
                      kind: str = "declared_violation") -> dict:
        """Emit a DECLARED-contract violation as the same gen_ai.agent.* schema (E11/E12).

        Declared violations are deterministic (no statistical score) — a span + a block signal, but
        no evaluation event. `gate.declared=true` distinguishes it from a statistical drift; `kind`
        separates the single-call case (`declared_violation`, E11) from the sequence case
        (`declared_sequence`, E12). C1: additive, no `drift.*`.
        """
        span_attrs: dict[str, object] = {
            A.GEN_AI_AGENT_ID: chain.agent_id,
            A.GEN_AI_AGENT_TASK_TYPE: chain.task_type,
            A.GEN_AI_TOOL_NAME: tool,
            A.GEN_AI_AGENT_TOOL_CATEGORY: category,
            A.GEN_AI_AGENT_TOOL_RISK_SEVERITY: risk,
            A.GEN_AI_AGENT_GATE_ACTION: "block",
            A.GEN_AI_AGENT_GATE_BLOCKED: True,
            A.GEN_AI_AGENT_GATE_REASON: reason,
            A.GEN_AI_AGENT_GATE_DECLARED: True,
            A.GEN_AI_AGENT_COMPUTED_ANOMALY: True,
            A.GEN_AI_AGENT_COMPUTED_ANOMALY_KIND: kind,
        }
        if self._tracer is not None:  # pragma: no cover - needs OTel + collector
            with self._tracer.start_as_current_span(A.execute_tool_span(tool)) as span:
                for k, v in span_attrs.items():
                    span.set_attribute(k, v)
        if self._m_decisions is not None:  # pragma: no cover - needs OTel + collector
            self._m_decisions.add(1, {"gate_action": "block"})
            self._m_anomaly.add(1, {"anomaly_kind": kind})
        return {"span": span_attrs, "declared": True, "reason": reason}

    def emit_cross_check(self, chain: DecisionChain, tool: str, *,
                         predicted: tuple[str, ...] = (), confidence: float = 0.0) -> dict:
        """Emit an E13 §4c prompt-aware cross-check divergence as a SHADOW `danger_detected` signal.

        The agent's selected `tool` diverged from a light LLM's prediction (`predicted`). Emit-only:
        `gate.action=log`, `gate.blocked=false`, `gate.declared=false` — this NEVER blocks (hard blocks
        stay declared); it is a learned, prompt-aware flag. C1: reuses the fixed gen_ai.agent.* schema;
        `gen_ai.evaluation.name = danger_detected`, `anomaly.kind = llm_cross_check_mismatch`.
        """
        kind = A.ANOMALY_LLM_CROSS_CHECK_MISMATCH
        span_attrs: dict[str, object] = {
            A.GEN_AI_AGENT_ID: chain.agent_id,
            A.GEN_AI_AGENT_TASK_TYPE: chain.task_type,
            A.GEN_AI_TOOL_NAME: tool,
            A.GEN_AI_AGENT_GATE_ACTION: "log",
            A.GEN_AI_AGENT_GATE_BLOCKED: False,
            A.GEN_AI_AGENT_GATE_DECLARED: False,
            A.GEN_AI_AGENT_COMPUTED_ANOMALY: True,
            A.GEN_AI_AGENT_COMPUTED_ANOMALY_KIND: kind,
        }
        if self._tracer is not None:  # pragma: no cover - needs OTel + collector
            with self._tracer.start_as_current_span(A.execute_tool_span(tool)) as span:
                for k, v in span_attrs.items():
                    span.set_attribute(k, v)
                # C1: the score lives on the fixed gen_ai.evaluation.result event (constant names only)
                span.add_event(A.GEN_AI_EVALUATION_RESULT_EVENT, attributes={
                    A.GEN_AI_EVALUATION_NAME: A.EVAL_DANGER_DETECTED,
                    A.GEN_AI_EVALUATION_SCORE_VALUE: confidence,
                    A.GEN_AI_EVALUATION_EXPLANATION: f"cross-check: predicted {list(predicted)}",
                })
        if self._m_decisions is not None:  # pragma: no cover - needs OTel + collector
            self._m_decisions.add(1, {"gate_action": "log"})
            self._m_anomaly.add(1, {"anomaly_kind": kind})
        return {"cross_check": True, "predicted": list(predicted), "confidence": confidence,
                "anomaly_kind": kind}
