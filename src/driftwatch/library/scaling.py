"""Inverse-scaling detector: is a bigger model actually safer?

Adapted from the Observability Summit `observation/scaling.py`. Regresses drift
severity on log2(model capability). A positive slope (beta1 > 0) means larger models
drift *more*, not less — surfaced as the `inverse_scaling_trend` evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScalingResult:
    beta0: float
    beta1: float
    r_squared: float
    n: int
    inverse_scaling: bool       # beta1 > 0 -> bigger models drift more


def ols_inverse_scaling(capabilities: list[float], deviations: list[float]) -> ScalingResult | None:
    """OLS of deviation ~ beta0 + beta1*log2(capability). None if too few points."""
    import numpy as np

    if len(capabilities) < 20 or len(capabilities) != len(deviations):
        return None
    caps = np.asarray(capabilities, dtype=float)
    devs = np.asarray(deviations, dtype=float)
    log_caps = np.log2(np.maximum(caps, 0.1))

    x_mean, y_mean = float(np.mean(log_caps)), float(np.mean(devs))
    ss_xx = float(np.sum((log_caps - x_mean) ** 2))
    if ss_xx == 0.0:
        return ScalingResult(beta0=y_mean, beta1=0.0, r_squared=0.0, n=len(caps), inverse_scaling=False)
    ss_xy = float(np.sum((log_caps - x_mean) * (devs - y_mean)))
    beta1 = ss_xy / ss_xx
    beta0 = y_mean - beta1 * x_mean
    ss_res = float(np.sum((devs - (beta0 + beta1 * log_caps)) ** 2))
    ss_tot = float(np.sum((devs - y_mean) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return ScalingResult(beta0=beta0, beta1=beta1, r_squared=r2, n=len(caps), inverse_scaling=beta1 > 0)
