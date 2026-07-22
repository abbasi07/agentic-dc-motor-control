"""Evaluation scenarios for fair controller comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Scenario:
    """One simulation experiment run by the evaluation harness.

    reference(t) and load_torque(t) are callables.
    plant_scale multiplies nominal CTMS parameters (mismatch case).
    noise_std adds Gaussian measurement noise (rad/s).
    """

    name: str
    description: str
    t_final: float = 3.0
    dt: float = 0.001
    reference: Callable[[float], float] = field(default=lambda t: 1.0)
    load_torque: Callable[[float], float] = field(default=lambda t: 0.0)
    plant_scale: dict[str, float] = field(default_factory=dict)
    noise_std: float = 0.0
    seed: int | None = 0


def default_scenarios() -> list[Scenario]:
    """Baseline scenario suite used for PID and future advanced controllers."""

    def step_ref(t: float) -> float:
        return 1.0

    def load_step(t: float) -> float:
        # Apply opposing load after the plant has roughly settled under PID
        return 0.05 if t >= 1.5 else 0.0

    return [
        Scenario(
            name="step_1rads",
            description="Unit step reference omega_ref = 1 rad/s, no disturbance.",
            reference=step_ref,
        ),
        Scenario(
            name="load_disturbance",
            description="Step reference then load torque 0.05 N·m at t = 1.5 s.",
            t_final=4.0,
            reference=step_ref,
            load_torque=load_step,
        ),
        Scenario(
            name="plant_mismatch",
            description="Step reference on mismatched plant (J×1.3, b×0.7, R×1.2).",
            reference=step_ref,
            plant_scale={"J": 1.3, "b": 0.7, "R": 1.2},
        ),
        Scenario(
            name="noisy_measurement",
            description="Step reference with Gaussian speed sensor noise.",
            reference=step_ref,
            noise_std=0.02,
            seed=0,
        ),
    ]
