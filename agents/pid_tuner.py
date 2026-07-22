"""Constraint-aware PID tuner tools (DesignSpec -> PID + scorecard + FailureDigest).

Hard rule: gains are proposed by search/optimization; every candidate is scored with
``evaluate_controller``. The LLM must not invent gains without this tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from scipy.optimize import differential_evolution, minimize

from dc_motor.controllers import PIDController
from dc_motor.evaluate import evaluate_controller, scorecard_to_json
from dc_motor.failure import FailureDigest, failure_digest_from_scorecard
from dc_motor.plant import CTMS_PARAMS, MotorParams
from dc_motor.scenarios import Scenario, scenarios_from_spec
from dc_motor.specs import DesignSpec

TuneMethod = Literal["grid", "scipy", "auto"]


@dataclass
class PIDGains:
    Kp: float
    Ki: float
    Kd: float

    def to_dict(self) -> dict[str, float]:
        return {"Kp": float(self.Kp), "Ki": float(self.Ki), "Kd": float(self.Kd)}


@dataclass
class TuneResult:
    """Result of a constraint-aware PID tune — ready for the orchestrator."""

    controller: PIDController
    gains: PIDGains
    scorecard: dict[str, Any]
    failure_digest: FailureDigest
    method: str
    n_evaluations: int
    objective: float
    history: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    def to_dict(self, *, include_scorecard_json: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "gains": self.gains.to_dict(),
            "method": self.method,
            "n_evaluations": self.n_evaluations,
            "objective": self.objective,
            "all_pass": self.failure_digest.all_pass,
            "failure_digest": self.failure_digest.to_dict(),
            "mean_scalar_score": self.scorecard.get("summary", {}).get("mean_scalar_score"),
            "history": self.history,
            "notes": self.notes,
            "controller_name": self.controller.name,
        }
        if include_scorecard_json:
            out["scorecard_json"] = scorecard_to_json(self.scorecard)
        return out


# Reasonable search box around the CTMS PID baseline (Kp=100, Ki=200, Kd=10)
DEFAULT_GAIN_BOUNDS: dict[str, tuple[float, float]] = {
    "Kp": (5.0, 400.0),
    "Ki": (0.0, 800.0),
    "Kd": (0.0, 80.0),
}

# Coarse grid for the non-LLM ablation baseline (fast, reproducible)
DEFAULT_GRID: dict[str, list[float]] = {
    "Kp": [40.0, 80.0, 100.0, 150.0, 220.0],
    "Ki": [50.0, 120.0, 200.0, 350.0, 500.0],
    "Kd": [0.0, 5.0, 10.0, 20.0, 40.0],
}

# Violation penalty scale for soft objective when constraints fail
_VIOLATION_PENALTY = 1e3


def _make_controller(
    gains: PIDGains,
    spec: DesignSpec,
    *,
    name: str | None = None,
) -> PIDController:
    label = name or f"PID_Kp{gains.Kp:.3g}_Ki{gains.Ki:.3g}_Kd{gains.Kd:.3g}"
    return PIDController(
        Kp=float(gains.Kp),
        Ki=float(gains.Ki),
        Kd=float(gains.Kd),
        V_min=spec.V_min,
        V_max=spec.V_max,
        name=label,
    )


def evaluate_pid_gains(
    gains: PIDGains,
    spec: DesignSpec,
    *,
    scenarios: list[Scenario] | None = None,
    base_params: MotorParams = CTMS_PARAMS,
    name: str | None = None,
    plant_factory=None,
) -> dict[str, Any]:
    """Evaluate one PID gain set against DesignSpec constraints/scenarios."""
    controller = _make_controller(gains, spec, name=name)
    scens = scenarios if scenarios is not None else scenarios_from_spec(spec)
    return evaluate_controller(
        controller,
        scenarios=scens,
        constraints=spec.constraints_for_evaluator(),
        score_weights=spec.score_weights_for_evaluator(),
        base_params=base_params,
        plant_factory=plant_factory,
    )


def scorecard_objective(scorecard: dict[str, Any]) -> float:
    """Constraints-first objective (lower is better).

    Pass: mean_scalar_score (soft preferences).
    Fail: large penalty + sum of positive violation margins.
    """
    digest = failure_digest_from_scorecard(scorecard)
    mean = float(scorecard.get("summary", {}).get("mean_scalar_score", 1e6))
    if digest.all_pass:
        return mean
    violation = 0.0
    for f in digest.failures:
        m = f.margin if np.isfinite(f.margin) else 1e3
        violation += max(0.0, float(m))
    return _VIOLATION_PENALTY * (1.0 + violation) + mean


def zn_warm_start(spec: DesignSpec) -> PIDGains:
    """Classical-ish warm start for CTMS speed loop (heuristic, not full ZN relay).

    Tuned near the CTMS working point; scales lightly with omega_ref / voltage.
    """
    # Baseline that works on CTMS unit-step with ±12 V
    kp0, ki0, kd0 = 100.0, 200.0, 10.0
    # Mild scale: higher |V| budget -> allow slightly more aggressive Kp
    v_scale = float(spec.V_max) / 12.0
    ref_scale = max(0.5, min(2.0, float(spec.omega_ref)))
    return PIDGains(
        Kp=kp0 * v_scale / ref_scale,
        Ki=ki0 * v_scale / ref_scale,
        Kd=kd0 * v_scale,
    )


def _pack(
    gains: PIDGains,
    spec: DesignSpec,
    scorecard: dict[str, Any],
    *,
    method: str,
    n_evaluations: int,
    history: list[dict[str, Any]] | None = None,
    notes: str = "",
) -> TuneResult:
    controller = _make_controller(gains, spec, name=scorecard.get("controller"))
    # Re-bind name from scorecard evaluation
    controller.name = str(scorecard.get("controller", controller.name))
    digest = failure_digest_from_scorecard(scorecard)
    return TuneResult(
        controller=controller,
        gains=gains,
        scorecard=scorecard,
        failure_digest=digest,
        method=method,
        n_evaluations=n_evaluations,
        objective=scorecard_objective(scorecard),
        history=list(history or []),
        notes=notes,
    )


def grid_search_pid(
    spec: DesignSpec,
    *,
    grid: dict[str, list[float]] | None = None,
    base_params: MotorParams = CTMS_PARAMS,
    stop_on_pass: bool | None = None,
    plant_factory=None,
) -> TuneResult:
    """Non-LLM baseline: enumerate a gain grid, pick constraints-first best."""
    g = grid or DEFAULT_GRID
    scenarios = scenarios_from_spec(spec)
    stop = spec.stop_on_pass if stop_on_pass is None else stop_on_pass

    best: TuneResult | None = None
    history: list[dict[str, Any]] = []
    n_eval = 0

    for kp in g["Kp"]:
        for ki in g["Ki"]:
            for kd in g["Kd"]:
                gains = PIDGains(Kp=float(kp), Ki=float(ki), Kd=float(kd))
                scorecard = evaluate_pid_gains(
                    gains,
                    spec,
                    scenarios=scenarios,
                    base_params=base_params,
                    plant_factory=plant_factory,
                )
                n_eval += 1
                obj = scorecard_objective(scorecard)
                digest = failure_digest_from_scorecard(scorecard)
                history.append(
                    {
                        "gains": gains.to_dict(),
                        "objective": obj,
                        "all_pass": digest.all_pass,
                        "mean_scalar_score": scorecard["summary"]["mean_scalar_score"],
                    }
                )
                cand = _pack(
                    gains,
                    spec,
                    scorecard,
                    method="grid",
                    n_evaluations=n_eval,
                    history=history,
                )
                if best is None or cand.objective < best.objective:
                    best = cand
                    if stop and digest.all_pass:
                        best.notes = (
                            f"Grid search stopped early on first pass "
                            f"(eval {n_eval}/{len(g['Kp']) * len(g['Ki']) * len(g['Kd'])})."
                        )
                        best.n_evaluations = n_eval
                        best.history = history
                        return best

    assert best is not None
    best.n_evaluations = n_eval
    best.history = history
    best.notes = f"Grid search complete: {n_eval} evaluations; best objective={best.objective:.4g}."
    return best


def optimize_pid(
    spec: DesignSpec,
    *,
    method: Literal["differential_evolution", "nelder-mead"] = "differential_evolution",
    bounds: dict[str, tuple[float, float]] | None = None,
    maxiter: int = 25,
    seed: int = 0,
    base_params: MotorParams = CTMS_PARAMS,
    warm_start: PIDGains | None = None,
    plant_factory=None,
) -> TuneResult:
    """SciPy optimize PID gains scored by evaluate_controller (constraint-aware objective)."""
    b = bounds or DEFAULT_GAIN_BOUNDS
    scenarios = scenarios_from_spec(spec)
    history: list[dict[str, Any]] = []
    n_eval = 0
    x0 = warm_start or zn_warm_start(spec)

    def _eval_vec(x: np.ndarray) -> float:
        nonlocal n_eval
        gains = PIDGains(Kp=float(x[0]), Ki=float(x[1]), Kd=float(x[2]))
        # Clamp soft (DE stays in bounds; Nelder-Mead may wander)
        gains = PIDGains(
            Kp=float(np.clip(gains.Kp, b["Kp"][0], b["Kp"][1])),
            Ki=float(np.clip(gains.Ki, b["Ki"][0], b["Ki"][1])),
            Kd=float(np.clip(gains.Kd, b["Kd"][0], b["Kd"][1])),
        )
        scorecard = evaluate_pid_gains(
            gains,
            spec,
            scenarios=scenarios,
            base_params=base_params,
            plant_factory=plant_factory,
        )
        n_eval += 1
        obj = scorecard_objective(scorecard)
        digest = failure_digest_from_scorecard(scorecard)
        history.append(
            {
                "gains": gains.to_dict(),
                "objective": obj,
                "all_pass": digest.all_pass,
                "mean_scalar_score": scorecard["summary"]["mean_scalar_score"],
            }
        )
        return obj

    lo = [b["Kp"][0], b["Ki"][0], b["Kd"][0]]
    hi = [b["Kp"][1], b["Ki"][1], b["Kd"][1]]

    if method == "differential_evolution":
        # Keep population small for demo-friendly runtimes
        result = differential_evolution(
            _eval_vec,
            bounds=list(zip(lo, hi)),
            maxiter=maxiter,
            popsize=6,
            seed=seed,
            polish=False,
            updating="immediate",
            workers=1,
            atol=1e-3,
            tol=1e-2,
        )
        x_best = result.x
        opt_name = "scipy_de"
    else:
        result = minimize(
            _eval_vec,
            x0=np.array([x0.Kp, x0.Ki, x0.Kd], dtype=float),
            method="Nelder-Mead",
            options={"maxiter": maxiter * 10, "xatol": 1e-2, "fatol": 1e-2},
        )
        x_best = result.x
        opt_name = "scipy_nm"

    best_gains = PIDGains(
        Kp=float(np.clip(x_best[0], lo[0], hi[0])),
        Ki=float(np.clip(x_best[1], lo[1], hi[1])),
        Kd=float(np.clip(x_best[2], lo[2], hi[2])),
    )
    # Final scored evaluation (also if last history point was clipped differently)
    scorecard = evaluate_pid_gains(
        best_gains,
        spec,
        scenarios=scenarios,
        base_params=base_params,
        plant_factory=plant_factory,
    )
    n_eval += 1
    return _pack(
        best_gains,
        spec,
        scorecard,
        method=opt_name,
        n_evaluations=n_eval,
        history=history,
        notes=f"SciPy {method}: success={getattr(result, 'success', None)}, n_eval={n_eval}.",
    )


def tune_pid(
    spec: DesignSpec,
    *,
    method: TuneMethod = "auto",
    base_params: MotorParams = CTMS_PARAMS,
    grid: dict[str, list[float]] | None = None,
    maxiter: int = 20,
    seed: int = 0,
    plant_factory=None,
) -> TuneResult:
    """Main tool entry: DesignSpec -> candidate PID + scorecard + FailureDigest.

    ``auto``: run grid baseline first; if it fails hard constraints, refine with
    differential evolution (warm-started from the grid winner).
    """
    if method == "grid":
        return grid_search_pid(
            spec, grid=grid, base_params=base_params, plant_factory=plant_factory
        )

    if method == "scipy":
        return optimize_pid(
            spec,
            method="differential_evolution",
            maxiter=maxiter,
            seed=seed,
            base_params=base_params,
            plant_factory=plant_factory,
        )

    # auto
    grid_result = grid_search_pid(
        spec,
        grid=grid,
        base_params=base_params,
        stop_on_pass=True,
        plant_factory=plant_factory,
    )
    if grid_result.failure_digest.all_pass:
        grid_result.method = "auto_grid"
        grid_result.notes = (grid_result.notes + " Auto: grid already passed.").strip()
        return grid_result

    refined = optimize_pid(
        spec,
        method="differential_evolution",
        maxiter=maxiter,
        seed=seed,
        base_params=base_params,
        warm_start=grid_result.gains,
        plant_factory=plant_factory,
    )
    # Prefer refined if better objective; else keep grid
    if refined.objective <= grid_result.objective:
        refined.method = "auto_scipy"
        refined.n_evaluations = grid_result.n_evaluations + refined.n_evaluations
        refined.history = list(grid_result.history) + list(refined.history)
        refined.notes = (
            f"Auto: grid failed ({grid_result.failure_digest.n_failures} failures); "
            f"DE refined. {refined.notes}"
        )
        return refined

    grid_result.method = "auto_grid"
    grid_result.notes = (
        f"Auto: DE did not improve on grid (grid_obj={grid_result.objective:.4g}, "
        f"de_obj={refined.objective:.4g}). Kept grid winner."
    )
    grid_result.n_evaluations = grid_result.n_evaluations + refined.n_evaluations
    grid_result.history = list(grid_result.history) + list(refined.history)
    return grid_result


# Re-export for convenience
__all__ = [
    "PIDGains",
    "TuneResult",
    "DEFAULT_GAIN_BOUNDS",
    "DEFAULT_GRID",
    "evaluate_pid_gains",
    "scorecard_objective",
    "zn_warm_start",
    "grid_search_pid",
    "optimize_pid",
    "tune_pid",
    "failure_digest_from_scorecard",
    "FailureDigest",
]
