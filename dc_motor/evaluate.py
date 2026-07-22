"""Shared Simulation & Evaluation node — deterministic scorecards."""

from __future__ import annotations

import json
import math
from typing import Any, Protocol

import numpy as np

from .metrics import step_performance_metrics
from .plant import CTMS_PARAMS, DCMotorPlant, MotorParams
from .scenarios import Scenario, default_scenarios


class ControllerProtocol(Protocol):
    name: str

    def reset(self) -> None: ...

    def step(self, measurement: float, reference: float, dt: float) -> float: ...


# Soft constraints used until the user/orchestrator supplies custom ones
DEFAULT_CONSTRAINTS: dict[str, tuple[str, float]] = {
    "settling_time_s": ("<=", 2.0),
    "overshoot_pct": ("<=", 15.0),
    "steady_state_error": ("<=", 0.05),
}

# Lower-is-better weights for a simple scalar score (primary comparison key)
DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "ITAE": 1.0,
    "overshoot_pct": 0.05,
    "control_effort": 0.01,
    "saturation_time_s": 0.1,
}


def _check_constraints(metrics: dict[str, float], constraints: dict[str, tuple[str, float]]) -> dict:
    results = {}
    all_pass = True
    for key, (op, limit) in constraints.items():
        val = metrics.get(key, float("nan"))
        if val is None or (isinstance(val, float) and math.isnan(val)):
            ok = False
        elif op == "<=":
            ok = val <= limit
        elif op == ">=":
            ok = val >= limit
        elif op == "<":
            ok = val < limit
        elif op == ">":
            ok = val > limit
        else:
            raise ValueError(f"Unsupported constraint operator: {op}")
        results[key] = {"value": val, "op": op, "limit": limit, "pass": bool(ok)}
        all_pass = all_pass and ok
    return {"all_pass": all_pass, "checks": results}


def _scalar_score(metrics: dict[str, float], weights: dict[str, float]) -> float:
    score = 0.0
    for key, w in weights.items():
        val = metrics.get(key, 0.0)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            val = 1e6  # penalize missing metrics heavily
        score += w * float(val)
    return float(score)


def simulate_scenario(
    controller: ControllerProtocol,
    scenario: Scenario,
    base_params: MotorParams = CTMS_PARAMS,
) -> dict[str, Any]:
    """Run one closed-loop scenario; return trajectories + metrics."""
    plant = DCMotorPlant(base_params)
    if scenario.plant_scale:
        plant = plant.with_mismatch(**scenario.plant_scale)

    controller.reset()
    plant.reset()

    n = int(np.round(scenario.t_final / scenario.dt)) + 1
    t = np.linspace(0.0, scenario.t_final, n)
    rng = np.random.default_rng(scenario.seed)

    omega = np.zeros(n)
    u_hist = np.zeros(n)
    e_hist = np.zeros(n)
    ref_hist = np.zeros(n)
    sat = np.zeros(n, dtype=bool)

    for k, tk in enumerate(t):
        ref = float(scenario.reference(tk))
        true_omega = plant.omega
        meas = true_omega
        if scenario.noise_std > 0.0:
            meas = meas + float(rng.normal(0.0, scenario.noise_std))

        u = float(controller.step(meas, ref, scenario.dt))
        saturated = bool(getattr(controller, "last_saturated", False))

        # Log sample k before plant update (matches Lab_01 convention)
        omega[k] = true_omega
        u_hist[k] = u
        ref_hist[k] = ref
        e_hist[k] = ref - true_omega
        sat[k] = saturated

        tau = float(scenario.load_torque(tk))
        plant.step(u, scenario.dt, load_torque=tau)

    # For step-like refs, evaluate tracking metrics against the final reference value
    y_ref = float(scenario.reference(scenario.t_final))
    metrics = step_performance_metrics(t, omega, u_hist, e_hist, sat, y_ref)

    return {
        "scenario": scenario.name,
        "description": scenario.description,
        "t": t,
        "omega": omega,
        "u": u_hist,
        "e": e_hist,
        "reference": ref_hist,
        "saturated": sat,
        "metrics": metrics,
    }


def evaluate_controller(
    controller: ControllerProtocol,
    scenarios: list[Scenario] | None = None,
    constraints: dict[str, tuple[str, float]] | None = None,
    score_weights: dict[str, float] | None = None,
    base_params: MotorParams = CTMS_PARAMS,
) -> dict[str, Any]:
    """Simulation & Evaluation Node.

    Returns a JSON-serializable scorecard (arrays excluded from JSON helper).
    """
    scenarios = scenarios if scenarios is not None else default_scenarios()
    constraints = constraints if constraints is not None else DEFAULT_CONSTRAINTS
    score_weights = score_weights if score_weights is not None else DEFAULT_SCORE_WEIGHTS

    per_scenario = []
    for sc in scenarios:
        result = simulate_scenario(controller, sc, base_params=base_params)
        constraint_result = _check_constraints(result["metrics"], constraints)
        scalar = _scalar_score(result["metrics"], score_weights)
        per_scenario.append(
            {
                "name": sc.name,
                "description": sc.description,
                "metrics": result["metrics"],
                "constraints": constraint_result,
                "scalar_score": scalar,
                "trajectories": {
                    "t": result["t"],
                    "omega": result["omega"],
                    "u": result["u"],
                    "e": result["e"],
                    "reference": result["reference"],
                    "saturated": result["saturated"],
                },
            }
        )

    all_pass = all(item["constraints"]["all_pass"] for item in per_scenario)
    # Aggregate: mean scalar score across scenarios (lower is better)
    mean_score = float(np.mean([item["scalar_score"] for item in per_scenario]))

    return {
        "controller": getattr(controller, "name", controller.__class__.__name__),
        "constraints": {k: {"op": v[0], "limit": v[1]} for k, v in constraints.items()},
        "score_weights": score_weights,
        "scenarios": per_scenario,
        "summary": {
            "all_constraints_pass": all_pass,
            "mean_scalar_score": mean_score,
            "n_scenarios": len(per_scenario),
        },
    }


def scorecard_to_json(scorecard: dict[str, Any], indent: int = 2) -> str:
    """Serialize scorecard without trajectory arrays (LLM/orchestrator friendly)."""

    def _sanitize(obj):
        if isinstance(obj, dict):
            return {
                k: _sanitize(v)
                for k, v in obj.items()
                if k != "trajectories"
            }
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        if isinstance(obj, (np.floating, float)):
            val = float(obj)
            if math.isnan(val) or math.isinf(val):
                return None
            return val
        if isinstance(obj, (np.integer, int)):
            return int(obj)
        if isinstance(obj, (np.bool_, bool)):
            return bool(obj)
        return obj

    return json.dumps(_sanitize(scorecard), indent=indent)
