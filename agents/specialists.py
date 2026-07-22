"""Specialist controller designers (workstream C).

Each ``design_*`` function turns a :class:`DesignSpec` (+ motor params) into a
scored :class:`DesignCandidate` using the deterministic evaluation harness. The
controllers themselves live in :mod:`agents.controllers_advanced` (real LQR/LQG,
constrained MPC, MRAC, Fuzzy PID) and in :mod:`dc_motor.controllers` (PID); all
share ``reset()`` / ``step(measurement, reference, dt) -> u``.

Hard rule (unchanged): tools COMPUTE every metric via ``evaluate_controller``.
The LLM never invents gains, matrices, or pass/fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from dc_motor.controllers import PIDController
from dc_motor.evaluate import evaluate_controller
from dc_motor.motor_model import motor_characteristics
from dc_motor.plant import CTMS_PARAMS, DCMotorPlant, MotorParams
from dc_motor.scenarios import scenarios_from_spec
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec
from dc_motor.state_space import motor_state_space

from .controllers_advanced import (
    FuzzyPIDController,
    MPCController,
    MRACController,
    StateFeedbackServoController,
    kalman_observer_gain,
    luenberger_observer_gain,
)
from .design_candidate import DesignCandidate, candidate_from_controller
from .pid_tuner import PIDGains, tune_pid, zn_warm_start

_NOMINAL_DT = 1e-3


# ---------------------------------------------------------------------------
# Shared scoring helper
# ---------------------------------------------------------------------------
def _score(
    ctrl: Any,
    spec: DesignSpec,
    *,
    kind: str,
    params: dict[str, Any],
    method: str,
    notes: str,
    base_params: MotorParams,
    plant_factory,
    n_evaluations: int = 1,
) -> DesignCandidate:
    scorecard = evaluate_controller(
        ctrl,
        scenarios=scenarios_from_spec(spec),
        constraints=spec.constraints_for_evaluator(),
        score_weights=spec.score_weights_for_evaluator(),
        base_params=base_params,
        plant_factory=plant_factory,
    )
    return candidate_from_controller(
        ctrl,
        scorecard,
        kind=kind,
        params=params,
        method=method,
        n_evaluations=n_evaluations,
        notes=notes,
    )


def _settling_target(spec: DesignSpec, default: float = 2.0) -> float:
    """Read the settling-time hard constraint (falls back to a sane default)."""
    st = spec.hard_constraints.get("settling_time_s")
    if st is not None:
        try:
            return float(st[1])
        except (TypeError, ValueError, IndexError):
            pass
    return default


# ---------------------------------------------------------------------------
# Legacy adaptive PID (kept for backward-compatible imports; design_adaptive now
# builds a real MRAC controller — see design_mrac).
# ---------------------------------------------------------------------------
@dataclass
class AdaptivePIDController:
    """Deprecated Ki-lite adaptive PID. Retained only for import stability."""

    Kp: float = 80.0
    Ki: float = 150.0
    Kd: float = 5.0
    Ki_max: float = 600.0
    adapt_rate: float = 40.0
    V_min: float = -12.0
    V_max: float = 12.0
    name: str = "AdaptivePID"

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._integ = 0.0
        self._e_prev = 0.0
        self._initialized = False
        self._Ki = float(self.Ki)
        self.last_saturated = False

    def step(self, measurement: float, reference: float, dt: float) -> float:
        e = reference - measurement
        self._Ki = min(self.Ki_max, max(0.0, self._Ki + self.adapt_rate * abs(e) * dt))
        de = (e - self._e_prev) / dt if self._initialized else 0.0
        u_unsat = self.Kp * e + self._Ki * self._integ + self.Kd * de
        u = float(min(self.V_max, max(self.V_min, u_unsat)))
        saturated = u != u_unsat
        self.last_saturated = saturated
        if not saturated or (u_unsat > self.V_max and e < 0) or (u_unsat < self.V_min and e > 0):
            self._integ += e * dt
        self._e_prev = e
        self._initialized = True
        return u


# ---------------------------------------------------------------------------
# Robust PID (unchanged behaviour)
# ---------------------------------------------------------------------------
def design_robust_pid(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    seed: int = 0,
    plant_factory=None,
) -> DesignCandidate:
    """Detuned / robust PID: tune emphasizing mismatch (+ optional mismatch_load)."""
    scenarios = list(spec.required_scenarios)
    for name in ("plant_mismatch", "mismatch_harsh", "noisy_measurement"):
        if name not in scenarios:
            scenarios.append(name)
    robust_spec = DesignSpec(
        raw_spec=spec.raw_spec,
        hard_constraints=dict(spec.hard_constraints),
        soft_preferences={
            **dict(spec.soft_preferences),
            "overshoot_pct": max(0.1, float(spec.soft_preferences.get("overshoot_pct", 0.05))),
            "control_effort": max(0.05, float(spec.soft_preferences.get("control_effort", 0.01))),
        },
        required_scenarios=scenarios,
        omega_ref=spec.omega_ref,
        V_min=spec.V_min,
        V_max=spec.V_max,
        t_final=spec.t_final,
        max_design_iterations=spec.max_design_iterations,
        stop_on_pass=spec.stop_on_pass,
        source=spec.source,
        notes=spec.notes + " | robust_focus",
        warnings=list(spec.warnings),
    )
    robust_spec = validate_and_clamp_design_spec(robust_spec)
    tuned = tune_pid(
        robust_spec,
        method="auto",
        maxiter=8,
        seed=seed,
        base_params=base_params,
        plant_factory=plant_factory,
    )
    gains = PIDGains(
        Kp=float(tuned.gains.Kp) * 0.75,
        Ki=float(tuned.gains.Ki) * 0.9,
        Kd=float(tuned.gains.Kd) * 0.6,
    )
    ctrl = PIDController(
        Kp=gains.Kp,
        Ki=gains.Ki,
        Kd=gains.Kd,
        V_min=spec.V_min,
        V_max=spec.V_max,
        name=f"RobustPID_Kp{gains.Kp:.3g}",
    )
    return _score(
        ctrl,
        spec,
        kind="robust_pid",
        params=gains.to_dict(),
        method="robust_detuned",
        notes=f"Robust specialist: mismatch-focused tune then detune. {tuned.notes}",
        base_params=base_params,
        plant_factory=plant_factory,
        n_evaluations=tuned.n_evaluations + 1,
    )


# ---------------------------------------------------------------------------
# LQR / LQG — integral-augmented optimal state feedback (python-control)
# ---------------------------------------------------------------------------
def _design_lqi_gains(
    spec: DesignSpec, base_params: MotorParams
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float, dict[str, Any]]:
    """Solve the integral-augmented LQR. Returns A, B, C, Kx, ki, meta."""
    from control import lqr

    sm = motor_state_space(base_params)
    A, B, C = sm.A, sm.B, sm.C
    n = A.shape[0]

    # Integral-augmented plant: z = [x; xi], ẋi = -C x (reference injected online).
    Az = np.block([[A, np.zeros((n, 1))], [-C, np.zeros((1, 1))]])
    Bz = np.vstack([B, np.zeros((1, 1))])

    omega_ref = max(float(spec.omega_ref), 1e-3)
    settle = _settling_target(spec)
    # Bryson-style base weights, scaled by the tracking-speed demand via the
    # integral penalty (tighter settling -> heavier integral weight).
    speed_gain = (2.0 / max(settle, 0.1)) ** 2
    Q = np.diag([1.0, 25.0 / omega_ref**2, 4000.0 * speed_gain / omega_ref**2])
    R = np.array([[1.0 / max(float(spec.V_max), 1.0) ** 2]])

    K, _S, _E = lqr(Az, Bz, Q, R)
    K = np.asarray(K, dtype=float).reshape(1, -1)
    Kx = K[:, :n]
    ki = float(K[0, n])
    meta = {
        "Kx": Kx.ravel().tolist(),
        "ki": ki,
        "Q_diag": np.diag(Q).tolist(),
        "R": float(R[0, 0]),
        "settle_target_s": settle,
    }
    return A, B, C, Kx, ki, meta


def design_lqr(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """LQR (integral-augmented) with a deterministic Luenberger observer."""
    A, B, C, Kx, ki, meta = _design_lqi_gains(spec, base_params)
    L = luenberger_observer_gain(A, C, dt_nom=_NOMINAL_DT)
    ctrl = StateFeedbackServoController(
        A=A, B=B, C=C, Kx=Kx, ki=ki, L=L,
        V_min=spec.V_min, V_max=spec.V_max, name="LQR",
    )
    meta["observer"] = "luenberger"
    meta["observer_gain"] = L.ravel().tolist()
    return _score(
        ctrl, spec,
        kind="lqr",
        params=meta,
        method="lqi_luenberger",
        notes="Integral-augmented LQR (python-control) with pole-placed observer.",
        base_params=base_params,
        plant_factory=plant_factory,
    )


def design_lqg(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """LQG: the LQI gain plus a Kalman observer (robust to measurement noise)."""
    A, B, C, Kx, ki, meta = _design_lqi_gains(spec, base_params)
    # Heavier measurement-noise assumption if the spec exercises noisy scenarios.
    noisy = any(s.startswith("noise") or s == "noisy_measurement" for s in spec.required_scenarios)
    meas_var = 4e-4 if noisy else 1e-4
    L = kalman_observer_gain(A, C, process_var=1.0, meas_var=meas_var)
    ctrl = StateFeedbackServoController(
        A=A, B=B, C=C, Kx=Kx, ki=ki, L=L,
        V_min=spec.V_min, V_max=spec.V_max, name="LQG",
    )
    meta["observer"] = "kalman"
    meta["observer_gain"] = L.ravel().tolist()
    meta["meas_var"] = meas_var
    return _score(
        ctrl, spec,
        kind="lqg",
        params=meta,
        method="lqi_kalman",
        notes="Integral-augmented LQR with a steady-state Kalman filter (LQG).",
        base_params=base_params,
        plant_factory=plant_factory,
    )


# ---------------------------------------------------------------------------
# Constrained MPC (cvxpy + OSQP) — replaces the old toy line-search MPC
# ---------------------------------------------------------------------------
def design_mpc(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    horizon: int | None = None,
    plant_factory=None,
) -> DesignCandidate:
    """Proper constrained MPC: receding-horizon QP with hard voltage bounds."""
    sm = motor_state_space(base_params)
    chars = motor_characteristics(base_params, V_max=spec.V_max)
    tau_mech = float(chars.get("tau_mech_s", 0.1))
    if not np.isfinite(tau_mech) or tau_mech <= 0:
        tau_mech = 0.1

    # Digital control period: fast enough to be a good ZOH, coarse enough to keep
    # the horizon (and QP) small; horizon spans a few mechanical time constants.
    Ts = float(np.clip(tau_mech / 10.0, 5 * _NOMINAL_DT, 0.05))
    N = int(np.clip(round(5.0 * tau_mech / Ts), 15, 40)) if horizon is None else int(horizon)

    dsm = sm.discretize(Ts)
    L = luenberger_observer_gain(sm.A, sm.C, dt_nom=_NOMINAL_DT)
    r_u = 1e-3 / max(float(spec.V_max), 1.0) ** 2 * 144.0  # normalize around 12 V

    ctrl = MPCController(
        Ad=dsm.Ad, Bd=dsm.Bd, Cd=dsm.Cd,
        A_cont=sm.A, B_cont=sm.B, L=L,
        Ts=Ts, horizon=N,
        q_track=1.0, r_u=r_u, r_du=1e-2,
        V_min=spec.V_min, V_max=spec.V_max,
        name=f"MPC_N{N}",
    )
    params = {
        "Ts_s": Ts,
        "horizon": N,
        "q_track": 1.0,
        "r_u": r_u,
        "r_du": 1e-2,
        "V_min": spec.V_min,
        "V_max": spec.V_max,
        "solver": "OSQP",
    }
    return _score(
        ctrl, spec,
        kind="mpc",
        params=params,
        method="constrained_qp_mpc",
        notes=(
            f"Constrained MPC (cvxpy/OSQP): N={N}, Ts={Ts:.4g}s, hard |u|<=V_max "
            "enforced in the optimizer."
        ),
        base_params=base_params,
        plant_factory=plant_factory,
    )


# ---------------------------------------------------------------------------
# MRAC — model-reference adaptive control
# ---------------------------------------------------------------------------
def design_mrac(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """Lyapunov MRAC tracking a first-order reference model."""
    chars = motor_characteristics(base_params, V_max=spec.V_max)
    dc_gain = float(chars.get("dc_gain_rad_s_per_V", 0.1)) or 0.1
    tau_mech = float(chars.get("tau_mech_s", 0.1))
    if not np.isfinite(tau_mech) or tau_mech <= 0:
        tau_mech = 0.1

    settle = _settling_target(spec)
    tau_ref = max(settle / 4.0, 1.5 * tau_mech)  # don't demand faster than the plant
    a_m = 1.0 / tau_ref

    kr0 = 1.0 / dc_gain  # feedforward to reach steady state immediately
    # Adaptation rate is a bounded tuning knob (regressor is normalized online).
    gamma = 5.0

    ctrl = MRACController(
        a_m=a_m, kr0=kr0, ky0=0.0,
        gamma_r=gamma, gamma_y=gamma, sigma=1e-3,
        V_min=spec.V_min, V_max=spec.V_max, name="MRAC",
    )
    params = {
        "a_m": a_m,
        "tau_ref_s": tau_ref,
        "kr0": kr0,
        "gamma": gamma,
        "sigma": 1e-3,
        "dc_gain": dc_gain,
    }
    return _score(
        ctrl, spec,
        kind="mrac",
        params=params,
        method="lyapunov_mrac",
        notes=(
            f"MRAC: first-order reference model (tau_ref={tau_ref:.4g}s), Lyapunov "
            "gain adaptation with sigma-modification (sim-only online learning)."
        ),
        base_params=base_params,
        plant_factory=plant_factory,
    )


def design_adaptive(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """Adaptive family: now a proper MRAC (upgraded from the Ki-lite PID)."""
    return design_mrac(spec, base_params=base_params, plant_factory=plant_factory)


# ---------------------------------------------------------------------------
# Fuzzy PID — Takagi–Sugeno gain scheduling
# ---------------------------------------------------------------------------
def design_fuzzy(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """Fuzzy gain-scheduling PID built on a classical warm start."""
    warm = zn_warm_start(spec)
    ctrl = FuzzyPIDController(
        Kp=warm.Kp,
        Ki=warm.Ki,
        Kd=warm.Kd,
        e_scale=max(float(spec.omega_ref), 1e-3),
        V_min=spec.V_min,
        V_max=spec.V_max,
        name="FuzzyPID",
    )
    params = {
        "Kp0": warm.Kp,
        "Ki0": warm.Ki,
        "Kd0": warm.Kd,
        "e_scale": max(float(spec.omega_ref), 1e-3),
        "rules": "TS: SMALL/MEDIUM/LARGE error -> Kp/Ki/Kd multipliers",
    }
    return _score(
        ctrl, spec,
        kind="fuzzy_pid",
        params=params,
        method="fuzzy_gain_schedule",
        notes="Fuzzy PID: triangular error memberships schedule Kp/Ki/Kd online.",
        base_params=base_params,
        plant_factory=plant_factory,
    )


# ---------------------------------------------------------------------------
# Plant identification (simulation only) — unchanged
# ---------------------------------------------------------------------------
def identify_plant_sim(
    *,
    base_params: MotorParams = CTMS_PARAMS,
    u_step: float = 6.0,
    t_final: float = 3.0,
    dt: float = 0.001,
    plant_factory=None,
) -> dict[str, Any]:
    """Open-loop identification experiment in simulation (no hardware)."""
    plant = plant_factory() if plant_factory is not None else DCMotorPlant(base_params)
    plant.reset()
    n = int(np.round(t_final / dt)) + 1
    t = np.linspace(0.0, t_final, n)
    omega = np.zeros(n)
    for k, _tk in enumerate(t):
        omega[k] = plant.omega
        plant.step(u_step, dt, load_torque=0.0)
    ss = float(omega[-1])
    gain = ss / u_step if abs(u_step) > 1e-9 else float("nan")
    target = 0.632 * ss
    idx = np.where(omega >= target)[0]
    tau = float(t[idx[0]]) if len(idx) else float("nan")
    return {
        "experiment": "open_loop_voltage_step",
        "u_step_V": u_step,
        "omega_ss": ss,
        "estimated_dc_gain": gain,
        "estimated_tau_s": tau,
        "nominal_params": {
            "J": base_params.J,
            "b": base_params.b,
            "K": base_params.K,
            "R": base_params.R,
            "L": base_params.L,
        },
        "notes": (
            "Simulation ID only — compares twin response to a first-order fit. "
            "Use MODEL_DISTRUST tags to decide whether to expand mismatch scenarios."
        ),
    }


def run_identify_plant(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """Plant-ID tool wrapped as a DesignCandidate (interim safe PID after ID)."""
    report = identify_plant_sim(base_params=base_params, plant_factory=plant_factory)
    warm = zn_warm_start(spec)
    ctrl = PIDController(
        Kp=warm.Kp * 0.7,
        Ki=warm.Ki * 0.8,
        Kd=warm.Kd * 0.5,
        V_min=spec.V_min,
        V_max=spec.V_max,
        name="PID_after_ID",
    )
    params = warm.to_dict()
    params["plant_id"] = report
    return _score(
        ctrl, spec,
        kind="plant_id",
        params=params,
        method="sim_step_id",
        notes=f"Plant ID complete: gain={report['estimated_dc_gain']:.4g}, tau={report['estimated_tau_s']:.4g}s.",
        base_params=base_params,
        plant_factory=plant_factory,
    )


__all__ = [
    "AdaptivePIDController",
    "MPCController",
    "design_robust_pid",
    "design_lqr",
    "design_lqg",
    "design_mpc",
    "design_mrac",
    "design_adaptive",
    "design_fuzzy",
    "identify_plant_sim",
    "run_identify_plant",
]
