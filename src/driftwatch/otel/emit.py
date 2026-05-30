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
        # Only wire a live OTLP exporter when an endpoint is explicitly configured.
        # With no endpoint (tests, demos, offline scoring) we stay a pure dict builder —
        # no background export thread, no global-provider side effects.
        if not endpoint:
            return
        try:  # optional dependency — interceptor extra
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("driftwatch")
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
        return {"span": span_attrs, "event": event_attrs}
