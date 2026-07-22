"""Deterministic step-response and effort metrics."""

from __future__ import annotations

import math

import numpy as np


def _trapz(y, x) -> float:
    if hasattr(np, "trapezoid"):
        return float(np.trapezoid(y, x))
    return float(np.trapz(y, x))


def step_performance_metrics(
    t,
    y,
    u,
    e,
    saturated,
    y_ref,
    settle_band: float = 0.02,
    disturbance_onset_s: float | None = None,
) -> dict:
    """Compute standard step-response and effort metrics (JSON-friendly floats).

    If ``disturbance_onset_s`` is set, also report ``recovery_time_s`` =
    time from onset until the response last enters the settle band (NaN if never).
    """
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    u = np.asarray(u, dtype=float)
    e = np.asarray(e, dtype=float)
    saturated = np.asarray(saturated, dtype=bool)

    y10, y90 = 0.1 * y_ref, 0.9 * y_ref
    idx10 = np.where(y >= y10)[0]
    idx90 = np.where(y >= y90)[0]
    if len(idx10) and len(idx90) and idx90[0] >= idx10[0]:
        rise_time = float(t[idx90[0]] - t[idx10[0]])
    else:
        rise_time = float("nan")

    band = settle_band * abs(y_ref)
    outside = np.abs(y - y_ref) > band
    if np.any(outside):
        last_out = int(np.where(outside)[0][-1])
        settling_time = float(t[last_out + 1]) if last_out + 1 < len(t) else float("nan")
    else:
        settling_time = 0.0

    y_peak = float(np.max(y))
    overshoot_pct = (
        max(0.0, (y_peak - y_ref) / abs(y_ref) * 100.0) if y_ref != 0 else float("nan")
    )
    ss_error = float(abs(y_ref - y[-1]))

    if len(t) > 1:
        dt_arr = np.diff(t, prepend=t[0])
        dt_arr[0] = dt_arr[1] if len(dt_arr) > 1 else 0.0
        sat_time = float(np.sum(dt_arr[saturated]))
    else:
        sat_time = 0.0

    recovery_time = float("nan")
    if disturbance_onset_s is not None:
        onset = float(disturbance_onset_s)
        mask = t >= onset
        if np.any(mask):
            y_post = y[mask]
            t_post = t[mask]
            outside_post = np.abs(y_post - y_ref) > band
            if not np.any(outside_post):
                recovery_time = 0.0
            else:
                last_out = int(np.where(outside_post)[0][-1])
                if last_out + 1 < len(t_post):
                    recovery_time = float(t_post[last_out + 1] - onset)
                else:
                    recovery_time = float("nan")

    out = {
        "rise_time_s": rise_time,
        "settling_time_s": settling_time,
        "overshoot_pct": overshoot_pct,
        "steady_state_error": ss_error,
        "IAE": _trapz(np.abs(e), t),
        "ISE": _trapz(e**2, t),
        "ITAE": _trapz(t * np.abs(e), t),
        "control_effort": _trapz(np.abs(u), t),
        "saturation_time_s": sat_time,
        "recovery_time_s": recovery_time,
    }
    # Keep JSON friendly if recovery unused
    if disturbance_onset_s is None and math.isnan(recovery_time):
        out["recovery_time_s"] = float("nan")
    return out
