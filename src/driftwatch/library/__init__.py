"""DriftWatch detection core — shared by the operator and the interceptor.

Pure streaming statistics (z-score, n-gram). No OTel, no Kubernetes, no framework
coupling. The single source of truth for "did the agent drift?".
"""
from .baseline import BaselineStore, TaskBaseline
from .decision import Decision, score_chain
from .fingerprint import Fingerprint, fingerprint
from .ngram import NGramModel
from .scaling import ScalingResult, ols_inverse_scaling
from .zscore import StreamingZScore

__all__ = [
    "BaselineStore", "TaskBaseline",
    "Decision", "score_chain",
    "Fingerprint", "fingerprint",
    "NGramModel",
    "StreamingZScore",
    "ScalingResult", "ols_inverse_scaling",
]
