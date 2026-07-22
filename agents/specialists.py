"""Specialist design agents: Robust PID, simple MPC, adaptive, plant ID.

All controllers expose reset() / step(measurement, reference, dt) -> u.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from dc_motor.controllers import PIDController
from dc_motor.evaluate import evaluate_controller
from dc_motor.plant import CTMS_PARAMS, DCMotorPlant, MotorParams
from dc_motor.scenarios import scenarios_from_spec
from dc_motor.specs import DesignSpec

from .design_candidate import DesignCandidate, candidate_from_controller
from .pid_tuner import PIDGains, tune_pid, zn_warm_start


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------


@dataclass
class MPCController:
    """Short-horizon voltage MPC using a first-order speed model.

    Prediction uses discrete approx: omega+ = a*omega + b*u
    identified from CTMS DC gain / dominant time constant (simulation twin).
    Optimizes only the *current* input (control horizon 1) with input bounds.
    """

    V_min: float = -12.0
    V_max: float = 12.0
    horizon: int = 12
    q_track: float = 1.0
    r_u: float = 1e-4
    name: str = "MPC"

    # CTMS-ish discrete model (dt=0.001): slow mechanical mode
    a: float = 0.990
    b: float = 0.0010

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._omega_hat = 0.0
        self.last_saturated = False

    def step(self, measurement: float, reference: float, dt: float) -> float:
        # Mild online correction of state estimate
        self._omega_hat = 0.7 * self._omega_hat + 0.3 * float(measurement)
        best_u = 0.0
        best_cost = float("inf")
        # Coarse line search over admissible voltages (deterministic, no LLM)
        for u in np.linspace(self.V_min, self.V_max, 25):
            omega = float(self._omega_hat)
            cost = 0.0
            uu = float(u)
            for _ in range(self.horizon):
                omega = self.a * omega + self.b * uu
                err = float(reference) - omega
                cost += self.q_track * err * err + self.r_u * uu * uu
            if cost < best_cost:
                best_cost = cost
                best_u = uu
        u = float(min(self.V_max, max(self.V_min, best_u)))
        self.last_saturated = abs(u - best_u) > 1e-12 or abs(u) >= abs(self.V_max) - 1e-9
        # Advance internal model one step with applied u
        self._omega_hat = self.a * self._omega_hat + self.b * u
        return u


@dataclass
class AdaptivePIDController:
    """Simple adaptive PID: Ki grows with persistent error (MRAC-lite / learning).

    Not deep RL — simulation-only adaptive law that helps under load changes.
    """

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
        # Adaptation: increase Ki when |e| persists
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
# Design tools
# ---------------------------------------------------------------------------


def design_robust_pid(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    seed: int = 0,
    plant_factory=None,
) -> DesignCandidate:
    """Detuned / robust PID: tune emphasizing mismatch (+ optional mismatch_load)."""
    # Build a robustness-focused spec copy
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
    from dc_motor.specs import validate_and_clamp_design_spec

    robust_spec = validate_and_clamp_design_spec(robust_spec)
    tuned = tune_pid(
        robust_spec,
        method="auto",
        maxiter=8,
        seed=seed,
        base_params=base_params,
        plant_factory=plant_factory,
    )
    # Detune Kp/Kd for robustness margin
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
        kind="robust_pid",
        params=gains.to_dict(),
        method="robust_detuned",
        n_evaluations=tuned.n_evaluations + 1,
        notes=f"Robust specialist: mismatch-focused tune then detune. {tuned.notes}",
    )


def design_mpc(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    horizon: int = 12,
    plant_factory=None,
) -> DesignCandidate:
    """Instantiate and score a simple constraint-aware MPC controller."""
    ctrl = MPCController(
        V_min=spec.V_min,
        V_max=spec.V_max,
        horizon=horizon,
        name=f"MPC_h{horizon}",
    )
    # Rough CTMS discrete gain scaling with dt=0.001 (fixed in catalog)
    # Steady-state gain omega/V ≈ K/(bR+K^2) ≈ 0.1 for CTMS
    ctrl.a = 0.990
    ctrl.b = 0.0010 * (spec.V_max / 12.0)
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
        kind="mpc",
        params={"horizon": horizon, "a": ctrl.a, "b": ctrl.b, "V_max": spec.V_max},
        method="mpc_line_search",
        n_evaluations=1,
        notes="Simple finite-horizon MPC with input bounds (simulation twin model).",
    )


def design_adaptive(
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """Adaptive PID specialist for load-varying conditions."""
    warm = zn_warm_start(spec)
    ctrl = AdaptivePIDController(
        Kp=warm.Kp * 0.8,
        Ki=warm.Ki * 0.7,
        Kd=warm.Kd * 0.5,
        Ki_max=max(400.0, warm.Ki * 2.0),
        V_min=spec.V_min,
        V_max=spec.V_max,
        name="AdaptivePID",
    )
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
        kind="adaptive",
        params={
            "Kp": ctrl.Kp,
            "Ki0": ctrl.Ki,
            "Kd": ctrl.Kd,
            "Ki_max": ctrl.Ki_max,
            "adapt_rate": ctrl.adapt_rate,
        },
        method="adaptive_ki",
        n_evaluations=1,
        notes="Adaptive Ki law under persistent tracking error (sim-only learning).",
    )


def identify_plant_sim(
    *,
    base_params: MotorParams = CTMS_PARAMS,
    u_step: float = 6.0,
    t_final: float = 3.0,
    dt: float = 0.001,
    plant_factory=None,
) -> dict[str, Any]:
    """Open-loop identification experiment in simulation (no hardware).

    Returns estimated DC gain and approximate time constant from a voltage step.
    """
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
    """Plant-ID tool wrapped as a DesignCandidate (keeps prior controller if none).

    Does not invent a new feedback law; returns ID report in notes/params and
    re-scores a conservative robust PID as a safe interim controller.
    """
    report = identify_plant_sim(base_params=base_params, plant_factory=plant_factory)
    # Interim: slightly detuned baseline PID after ID
    warm = zn_warm_start(spec)
    ctrl = PIDController(
        Kp=warm.Kp * 0.7,
        Ki=warm.Ki * 0.8,
        Kd=warm.Kd * 0.5,
        V_min=spec.V_min,
        V_max=spec.V_max,
        name="PID_after_ID",
    )
    scorecard = evaluate_controller(
        ctrl,
        scenarios=scenarios_from_spec(spec),
        constraints=spec.constraints_for_evaluator(),
        score_weights=spec.score_weights_for_evaluator(),
        base_params=base_params,
        plant_factory=plant_factory,
    )
    params = warm.to_dict()
    params["plant_id"] = report
    return candidate_from_controller(
        ctrl,
        scorecard,
        kind="plant_id",
        params=params,
        method="sim_step_id",
        n_evaluations=1,
        notes=f"Plant ID complete: gain={report['estimated_dc_gain']:.4g}, tau={report['estimated_tau_s']:.4g}s.",
    )


__all__ = [
    "MPCController",
    "AdaptivePIDController",
    "design_robust_pid",
    "design_mpc",
    "design_adaptive",
    "identify_plant_sim",
    "run_identify_plant",
]
