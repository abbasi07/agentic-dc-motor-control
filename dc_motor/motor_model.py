"""Arbitrary DC-motor model: validation + transfer-function characteristics.

This generalizes the app beyond the fixed registry templates: a user can supply
*any* armature-controlled DC motor (J, b, K, R, L) and a voltage budget, and we

  1. validate the numbers are physically sane (positive, in believable ranges), and
  2. derive closed-form characteristics from the transfer function

        omega(s)     K
        -------- = --------------------------------------------
          V(s)     J L s^2 + (J R + b L) s + (b R + K^2)

so downstream feasibility checks and agents can reason about what the motor can
actually do *before* any controller tuning is attempted.

Physics only — no hardware, no LLM. ``MotorParams`` (from ``plant.py``) remains the
value object the simulation engine consumes; ``MotorModel`` wraps it with a voltage
budget, a name, and provenance for the SaaS/agent layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .plant import MotorParams

# Believable ranges for small/medium armature-controlled DC motors.
# Values outside these are *warnings* (unusual), not hard errors, unless <= 0.
PARAM_RANGES: dict[str, tuple[float, float]] = {
    "J": (1e-7, 10.0),      # rotor inertia [kg·m^2]
    "b": (1e-6, 10.0),      # viscous friction [N·m·s/rad]
    "K": (1e-4, 10.0),      # torque/back-emf constant [N·m/A = V·s/rad]
    "R": (1e-3, 1000.0),    # armature resistance [ohm]
    "L": (1e-7, 100.0),     # armature inductance [H]
}
V_RANGE: tuple[float, float] = (0.1, 1000.0)

PARAM_UNITS: dict[str, str] = {
    "J": "kg·m^2",
    "b": "N·m·s/rad",
    "K": "N·m/A",
    "R": "ohm",
    "L": "H",
}


class MotorModelValidationError(ValueError):
    """Raised when motor parameters are not physically usable (<=0, NaN, etc.)."""


@dataclass
class MotorModel:
    """A validated, simulatable DC motor plus its actuator voltage budget."""

    params: MotorParams
    V_max: float = 12.0
    name: str = "custom_dc_motor"
    source: str = "manual"  # manual | llm | preset
    notes: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def V_min(self) -> float:
        return -abs(self.V_max)

    def to_dict(self) -> dict[str, Any]:
        p = self.params
        return {
            "name": self.name,
            "source": self.source,
            "params": {"J": p.J, "b": p.b, "K": p.K, "R": p.R, "L": p.L},
            "param_units": dict(PARAM_UNITS),
            "V_max": self.V_max,
            "V_min": self.V_min,
            "notes": self.notes,
            "warnings": list(self.warnings),
            "characteristics": motor_characteristics(self.params, V_max=self.V_max),
        }


def _finite_positive(name: str, value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise MotorModelValidationError(f"{name} is not a number: {value!r}") from exc
    if math.isnan(v) or math.isinf(v):
        raise MotorModelValidationError(f"{name} must be finite, got {v}")
    if v <= 0.0:
        raise MotorModelValidationError(
            f"{name} must be > 0 for a physical DC motor, got {v} {PARAM_UNITS.get(name, '')}".strip()
        )
    return v


def validate_motor_params(
    J: float, b: float, K: float, R: float, L: float
) -> list[str]:
    """Raise on impossible values; return non-fatal warnings for unusual ones."""
    vals = {"J": J, "b": b, "K": K, "R": R, "L": L}
    checked = {name: _finite_positive(name, v) for name, v in vals.items()}

    warnings: list[str] = []
    for name, v in checked.items():
        lo, hi = PARAM_RANGES[name]
        if v < lo or v > hi:
            warnings.append(
                f"{name}={v:g} {PARAM_UNITS[name]} is outside the typical range "
                f"[{lo:g}, {hi:g}]; double-check units."
            )
    return warnings


def build_motor_model(
    *,
    J: float,
    b: float,
    K: float,
    R: float,
    L: float,
    V_max: float = 12.0,
    name: str = "custom_dc_motor",
    source: str = "manual",
    notes: str = "",
) -> MotorModel:
    """Validate raw numbers and package them into a ``MotorModel``.

    Raises ``MotorModelValidationError`` if any parameter is non-physical.
    """
    warnings = validate_motor_params(J, b, K, R, L)

    v = float(V_max)
    if math.isnan(v) or math.isinf(v) or v <= 0.0:
        raise MotorModelValidationError(f"V_max must be a positive voltage, got {V_max!r}")
    lo, hi = V_RANGE
    if v < lo or v > hi:
        warnings.append(f"V_max={v:g} V is outside the typical range [{lo:g}, {hi:g}] V.")

    params = MotorParams(J=float(J), b=float(b), K=float(K), R=float(R), L=float(L))
    return MotorModel(
        params=params,
        V_max=v,
        name=name or "custom_dc_motor",
        source=source,
        notes=notes,
        warnings=warnings,
    )


def motor_characteristics(params: MotorParams, *, V_max: float = 12.0) -> dict[str, Any]:
    """Closed-form open-loop characteristics from the DC-motor transfer function.

    All quantities are analytic (no simulation). Keys:

      dc_gain_rad_s_per_V : steady omega per volt = K / (bR + K^2)
      omega_max_rad_s     : steady no-load speed at +V_max
      tau_mech_s          : dominant (mechanical) time constant, J R / (bR + K^2)
      tau_elec_s          : electrical time constant, L / R
      wn_rad_s            : undamped natural frequency of the 2nd-order model
      zeta                : damping ratio
      damping             : "overdamped" | "critically" | "underdamped"
      poles_real/imag     : characteristic-polynomial roots
      settling_open_loop_s: ~4 / (zeta*wn) estimate of open-loop 2% settling
    """
    J, b, K, R, L = params.J, params.b, params.K, params.R, params.L

    denom0 = b * R + K * K  # constant term of char. poly
    dc_gain = K / denom0 if denom0 != 0 else float("inf")
    omega_max = dc_gain * abs(float(V_max))

    tau_mech = (J * R) / denom0 if denom0 != 0 else float("inf")
    tau_elec = L / R if R != 0 else float("inf")

    # J L s^2 + (J R + b L) s + (b R + K^2)
    a2 = J * L
    a1 = J * R + b * L
    a0 = denom0
    wn = math.sqrt(a0 / a2) if a2 > 0 and a0 > 0 else float("nan")
    zeta = a1 / (2.0 * math.sqrt(a2 * a0)) if a2 > 0 and a0 > 0 else float("nan")

    if math.isnan(zeta):
        damping = "unknown"
    elif zeta > 1.0 + 1e-9:
        damping = "overdamped"
    elif abs(zeta - 1.0) <= 1e-9:
        damping = "critically_damped"
    else:
        damping = "underdamped"

    # Roots of the characteristic polynomial
    disc = a1 * a1 - 4.0 * a2 * a0
    poles_real: list[float] = []
    poles_imag: list[float] = []
    if a2 > 0:
        if disc >= 0:
            sq = math.sqrt(disc)
            poles_real = [(-a1 + sq) / (2 * a2), (-a1 - sq) / (2 * a2)]
            poles_imag = [0.0, 0.0]
        else:
            sq = math.sqrt(-disc)
            re = -a1 / (2 * a2)
            im = sq / (2 * a2)
            poles_real = [re, re]
            poles_imag = [im, -im]

    settling_ol = float("nan")
    if not math.isnan(zeta) and zeta > 0 and not math.isnan(wn) and wn > 0:
        settling_ol = 4.0 / (zeta * wn)

    return {
        "dc_gain_rad_s_per_V": dc_gain,
        "omega_max_rad_s": omega_max,
        "tau_mech_s": tau_mech,
        "tau_elec_s": tau_elec,
        "wn_rad_s": wn,
        "zeta": zeta,
        "damping": damping,
        "poles_real": poles_real,
        "poles_imag": poles_imag,
        "settling_open_loop_s": settling_ol,
    }


__all__ = [
    "PARAM_RANGES",
    "PARAM_UNITS",
    "V_RANGE",
    "MotorModel",
    "MotorModelValidationError",
    "build_motor_model",
    "motor_characteristics",
    "validate_motor_params",
]
