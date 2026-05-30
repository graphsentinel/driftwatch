"""OTel emission — the gen_ai.agent.* decision-quality schema (Constraints C1)."""
from .emit import Emitter, build_evaluation_event, build_span_attributes

__all__ = ["Emitter", "build_span_attributes", "build_evaluation_event"]
