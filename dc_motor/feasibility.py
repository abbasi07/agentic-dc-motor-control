"""Physics-based feasibility of a DesignSpec against a specific DC motor.

The point of this module is requirement #2 of the product vision: *before* spending
any tuning budget, decide whether the user's performance targets are even physically
achievable on their motor with their voltage budget — and if not, say exactly why and
what to change.

Every check here is deterministic and derived from the motor transfer function
(see ``motor_model.motor_characteristics``). No LLM, no simulation. Agents may phrase
these findings conversationally, but they must not invent or override them.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .motor_model import motor_characteristics
from .plant import MotorParams
from .scenarios import LOAD_ONSET_S

Severity = str  # "error" | "warning" | "info"


@dataclass
class FeasibilityIssue:
    severity: Severity
    code: str
    message: str
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FeasibilityReport:
    """Result of checking a DesignSpec against a motor's physics."""

    feasible: bool  # False iff at least one severity == "error"
    issues: list[FeasibilityIssue] = field(default_factory=list)
    characteristics: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @property
    def errors(self) -> list[FeasibilityIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[FeasibilityIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "issues": [i.to_dict() for i in self.issues],
            "characteristics": dict(self.characteristics),
            "summary": self.summary,
        }

    def question_lines(self) -> list[str]:
        """Errors/warnings phrased as clarifying prompts for the spec critic."""
        lines: list[str] = []
        for issue in self.issues:
            if issue.severity == "info":
                continue
            text = issue.message
            if issue.suggestion:
                text = f"{text} {issue.suggestion}"
            lines.append(text)
        return lines


def _constraint_limit(hard_constraints: dict[str, Any], metric: str) -> tuple[str, float] | None:
    body = hard_constraints.get(metric)
    if body is None:
        return None
    if isinstance(body, dict):
        return str(body.get("op", "<=")), float(body["limit"])
    if isinstance(body, (list, tuple)) and len(body) == 2:
        return str(body[0]), float(body[1])
    return None


def min_time_to_reference(
    params: MotorParams, *, omega_ref: float, V_max: float
) -> float:
    """Optimistic lower bound on time to first reach ``omega_ref`` at full voltage.

    Neglects inductance and treats the applied torque as its maximal stall value
    (K * V_max / R), giving a constant acceleration K V_max / (R J). The true rise
    is always slower (back-emf, damping, the need to *settle*), so a requested
    settling/rise time below this bound is provably infeasible.
    """
    J, K, R = params.J, params.K, params.R
    accel_max = (K * V_max) / (R * J) if (R * J) != 0 else float("inf")
    if accel_max <= 0 or math.isinf(accel_max):
        return float("inf") if accel_max <= 0 else 0.0
    return abs(omega_ref) / accel_max


def analyze_feasibility(
    params: MotorParams,
    *,
    omega_ref: float,
    V_max: float,
    settling_limit: tuple[str, float] | None = None,
    rise_limit: tuple[str, float] | None = None,
    overshoot_limit: tuple[str, float] | None = None,
    required_scenarios: list[str] | None = None,
) -> FeasibilityReport:
    """Low-level feasibility analysis from explicit targets (no DesignSpec needed)."""
    chars = motor_characteristics(params, V_max=V_max)
    issues: list[FeasibilityIssue] = []

    omega_ref = abs(float(omega_ref))
    omega_max = float(chars["omega_max_rad_s"])
    t_reach = min_time_to_reference(params, omega_ref=omega_ref, V_max=V_max)

    # --- 1. Steady-state reachability ---------------------------------------
    if omega_ref >= omega_max:
        issues.append(
            FeasibilityIssue(
                "error",
                "REFERENCE_UNREACHABLE",
                f"Target speed {omega_ref:g} rad/s exceeds the motor's maximum steady "
                f"speed {omega_max:.4g} rad/s at ±{V_max:g} V.",
                suggestion=(
                    f"Raise the voltage budget above {omega_ref / max(omega_max, 1e-9) * V_max:.3g} V, "
                    f"or target a speed below {omega_max:.4g} rad/s."
                ),
            )
        )
    elif omega_ref > 0.9 * omega_max:
        issues.append(
            FeasibilityIssue(
                "warning",
                "REFERENCE_NEAR_CEILING",
                f"Target speed {omega_ref:g} rad/s is above 90% of the motor ceiling "
                f"{omega_max:.4g} rad/s; the actuator will saturate and there is little "
                "headroom to reject disturbances.",
                suggestion="Consider a larger voltage budget or a lower target speed.",
            )
        )

    # --- 2. Speed of response vs actuator authority -------------------------
    def _time_check(limit: tuple[str, float] | None, label: str, code: str) -> None:
        if limit is None:
            return
        op, lim = limit
        if op not in ("<=", "<"):
            return
        if lim < t_reach:
            issues.append(
                FeasibilityIssue(
                    "error",
                    f"{code}_INFEASIBLE",
                    f"Required {label} {op} {lim:g} s is faster than the motor can even "
                    f"reach {omega_ref:g} rad/s at full voltage (>= {t_reach:.4g} s).",
                    suggestion=(
                        f"Relax {label} to >= {2 * t_reach:.3g} s, or increase the voltage budget."
                    ),
                )
            )
        elif lim < 2.0 * t_reach:
            issues.append(
                FeasibilityIssue(
                    "warning",
                    f"{code}_TIGHT",
                    f"Required {label} {op} {lim:g} s is close to the physical limit "
                    f"(~{t_reach:.3g} s to reach the target at full voltage); expect heavy "
                    "saturation and possible overshoot.",
                    suggestion=f"A {label} of >= {2 * t_reach:.3g} s is more comfortable.",
                )
            )

    _time_check(settling_limit, "settling time", "SETTLING")
    _time_check(rise_limit, "rise time", "RISE")

    # --- 3. Overshoot vs settling trade-off ---------------------------------
    if overshoot_limit is not None and settling_limit is not None:
        os_op, os_lim = overshoot_limit
        se_op, se_lim = settling_limit
        if os_op in ("<=", "<") and se_op in ("<=", "<") and os_lim < 2.0 and se_lim < 1.5 * t_reach and t_reach > 0:
            issues.append(
                FeasibilityIssue(
                    "warning",
                    "OVERSHOOT_SETTLING_CONFLICT",
                    f"Very low overshoot ({os_lim:g}%) together with a near-minimal settling "
                    f"time ({se_lim:g} s) is hard: fast responses on this motor tend to overshoot.",
                    suggestion="Relax one of the two (a bit more overshoot, or a bit more time).",
                )
            )

    # --- 4. Load-disturbance onset vs absolute settling ---------------------
    scenarios = required_scenarios or []
    if settling_limit is not None and any(
        s in scenarios for s in ("load_disturbance", "mismatch_load")
    ):
        se_op, se_lim = settling_limit
        if se_op in ("<=", "<") and se_lim < LOAD_ONSET_S:
            issues.append(
                FeasibilityIssue(
                    "warning",
                    "SETTLING_BEFORE_LOAD_ONSET",
                    f"A load disturbance is applied at t={LOAD_ONSET_S:g} s, but settling "
                    f"is limited to {se_lim:g} s (absolute time); the metric cannot pass once "
                    "the load hits.",
                    suggestion=f"Use settling >= {LOAD_ONSET_S + 1.0:g} s on load tests, "
                    "or drop settling as a hard constraint for that scenario.",
                )
            )

    feasible = not any(i.severity == "error" for i in issues)
    n_err = sum(1 for i in issues if i.severity == "error")
    n_warn = sum(1 for i in issues if i.severity == "warning")
    if feasible and n_warn == 0:
        summary = (
            f"Feasible: target {omega_ref:g} rad/s is reachable (ceiling {omega_max:.4g} rad/s); "
            f"no conflicts detected."
        )
    elif feasible:
        summary = f"Feasible with {n_warn} caution(s); ceiling {omega_max:.4g} rad/s."
    else:
        summary = f"Infeasible: {n_err} physical conflict(s) must be resolved first."

    return FeasibilityReport(
        feasible=feasible, issues=issues, characteristics=chars, summary=summary
    )


def check_feasibility(
    params: MotorParams,
    spec,
    *,
    V_max: float | None = None,
) -> FeasibilityReport:
    """Feasibility of a ``DesignSpec`` against a motor (convenience wrapper).

    Prefer an explicit plant ``V_max`` when provided so characteristics / reachability
    use the motor actuator budget rather than a stale Operating Point default (±12 V).
    """
    hard = getattr(spec, "hard_constraints", {}) or {}
    v_budget = float(V_max) if V_max is not None else float(getattr(spec, "V_max", 12.0))
    return analyze_feasibility(
        params,
        omega_ref=float(getattr(spec, "omega_ref", 1.0)),
        V_max=v_budget,
        settling_limit=_constraint_limit(hard, "settling_time_s"),
        rise_limit=_constraint_limit(hard, "rise_time_s"),
        overshoot_limit=_constraint_limit(hard, "overshoot_pct"),
        required_scenarios=list(getattr(spec, "required_scenarios", []) or []),
    )


__all__ = [
    "FeasibilityIssue",
    "FeasibilityReport",
    "analyze_feasibility",
    "check_feasibility",
    "min_time_to_reference",
]
