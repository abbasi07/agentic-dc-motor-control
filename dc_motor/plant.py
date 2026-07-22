"""Armature-controlled DC motor plant (CTMS parameters)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MotorParams:
    """Physical parameters in SI units."""

    J: float  # kg·m^2
    b: float  # N·m·s/rad
    K: float  # Kt = Ke
    R: float  # ohm
    L: float  # H


# CTMS University of Michigan — DC Motor Speed example
CTMS_PARAMS = MotorParams(J=0.01, b=0.1, K=0.01, R=1.0, L=0.5)


class DCMotorPlant:
    """Continuous-time plant integrated with forward Euler.

    States: armature current i [A], angular speed omega [rad/s].
    Input: armature voltage u [V].
    Optional disturbance: load torque tau_L [N·m].
    """

    def __init__(self, params: MotorParams = CTMS_PARAMS):
        self.params = params
        self.reset()

    def reset(self) -> None:
        self.i = 0.0
        self.omega = 0.0

    def step(self, u: float, dt: float, load_torque: float = 0.0) -> float:
        p = self.params
        di = (-p.R * self.i - p.K * self.omega + u) / p.L
        domega = (p.K * self.i - p.b * self.omega - load_torque) / p.J
        self.i = self.i + di * dt
        self.omega = self.omega + domega * dt
        return self.omega

    def with_mismatch(self, **scale_or_value) -> "DCMotorPlant":
        """Return a new plant with scaled or overridden parameters.

        Examples:
            plant.with_mismatch(J=1.2)      # scale J by 1.2
            plant.with_mismatch(R=0.8)      # scale R by 0.8
        """
        p = self.params
        values = {"J": p.J, "b": p.b, "K": p.K, "R": p.R, "L": p.L}
        for key, factor in scale_or_value.items():
            if key not in values:
                raise KeyError(f"Unknown motor parameter: {key}")
            values[key] = values[key] * float(factor)
        return DCMotorPlant(MotorParams(**values))
