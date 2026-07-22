"""Controller interface and PID implementation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PIDController:
    """Discrete PID with voltage saturation and conditional anti-windup.

    Interface used by the evaluation harness (and future MPC/RL agents):
        reset() -> None
        step(measurement, reference, dt) -> u
    """

    Kp: float
    Ki: float
    Kd: float
    V_min: float = -12.0
    V_max: float = 12.0
    name: str = "PID"

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._integ = 0.0
        self._e_prev = 0.0
        self._initialized = False
        self.last_saturated = False

    def step(self, measurement: float, reference: float, dt: float) -> float:
        e = reference - measurement
        de = (e - self._e_prev) / dt if self._initialized else 0.0
        u_unsat = self.Kp * e + self.Ki * self._integ + self.Kd * de
        u = float(min(self.V_max, max(self.V_min, u_unsat)))
        saturated = u != u_unsat
        self.last_saturated = saturated

        if not saturated or (u_unsat > self.V_max and e < 0) or (u_unsat < self.V_min and e > 0):
            self._integ += e * dt

        self._e_prev = e
        self._initialized = True
        return u
