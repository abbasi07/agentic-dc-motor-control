"""State-space realization of the armature-controlled DC motor.

Deterministic plant physics (no LLM, no hardware) shared by the model-based
controllers (LQR / LQG / MPC). The state vector is

    x = [i, omega]         (armature current [A], angular speed [rad/s])
    u = V                  (armature voltage [V])
    y = omega              (measured output — the controlled speed)

from the same equations the simulation twin integrates:

    di/dt     = (-R i - K omega + V) / L
    domega/dt = ( K i - b omega - tau_L) / J

so a controller designed on this realization is consistent with
``dc_motor.plant.DCMotorPlant``. A load torque ``tau_L`` enters through the
disturbance matrix ``E`` (used by the observers / MPC as an output disturbance).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import cont2discrete

from .plant import CTMS_PARAMS, MotorParams


@dataclass(frozen=True)
class StateSpaceModel:
    """Continuous-time LTI model of the DC motor speed plant."""

    A: np.ndarray  # (2, 2) state matrix
    B: np.ndarray  # (2, 1) input matrix
    C: np.ndarray  # (1, 2) output matrix (measures omega)
    D: np.ndarray  # (1, 1) feedthrough (zero)
    E: np.ndarray  # (2, 1) load-torque disturbance matrix
    params: MotorParams

    @property
    def n_states(self) -> int:
        return int(self.A.shape[0])

    def discretize(self, dt: float) -> "DiscreteStateSpace":
        """Zero-order-hold discretization at sample time ``dt``."""
        Ad, Bd, Cd, Dd, _ = cont2discrete((self.A, self.B, self.C, self.D), dt, method="zoh")
        # Discretize the disturbance channel with the same ZOH.
        _, Ed, _, _, _ = cont2discrete((self.A, self.E, self.C, self.D), dt, method="zoh")
        return DiscreteStateSpace(
            Ad=np.asarray(Ad, dtype=float),
            Bd=np.asarray(Bd, dtype=float),
            Cd=np.asarray(Cd, dtype=float),
            Dd=np.asarray(Dd, dtype=float),
            Ed=np.asarray(Ed, dtype=float),
            dt=float(dt),
        )


@dataclass(frozen=True)
class DiscreteStateSpace:
    """Discrete-time LTI model at a fixed sample time."""

    Ad: np.ndarray
    Bd: np.ndarray
    Cd: np.ndarray
    Dd: np.ndarray
    Ed: np.ndarray
    dt: float

    @property
    def n_states(self) -> int:
        return int(self.Ad.shape[0])


def motor_state_space(params: MotorParams = CTMS_PARAMS) -> StateSpaceModel:
    """Build the continuous-time (A, B, C, D, E) realization for a motor."""
    J, b, K, R, L = params.J, params.b, params.K, params.R, params.L
    A = np.array([[-R / L, -K / L], [K / J, -b / J]], dtype=float)
    B = np.array([[1.0 / L], [0.0]], dtype=float)
    C = np.array([[0.0, 1.0]], dtype=float)
    D = np.array([[0.0]], dtype=float)
    E = np.array([[0.0], [-1.0 / J]], dtype=float)  # load torque disturbance
    return StateSpaceModel(A=A, B=B, C=C, D=D, E=E, params=params)


__all__ = [
    "StateSpaceModel",
    "DiscreteStateSpace",
    "motor_state_space",
]
