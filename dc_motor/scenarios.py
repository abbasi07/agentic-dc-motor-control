"""Evaluation scenarios for fair controller comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


LOAD_ONSET_S = 1.5
LOAD_TORQUE_NM = 0.01


@dataclass
class Scenario:
    """One simulation experiment run by the evaluation harness.

    reference(t) and load_torque(t) are callables.
    plant_scale multiplies nominal CTMS parameters (mismatch case).
    noise_std adds Gaussian measurement noise (rad/s).
    disturbance_onset_s enables recovery_time_s in metrics when set.
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
    disturbance_onset_s: float | None = None


def _step_ref(t: float) -> float:
    return 1.0


def _load_step(t: float, tau: float = LOAD_TORQUE_NM, onset: float = LOAD_ONSET_S) -> float:
    return tau if t >= onset else 0.0


def default_scenarios() -> list[Scenario]:
    """Baseline scenario suite used for PID and future advanced controllers."""
    return [
        Scenario(
            name="step_1rads",
            description="Unit step reference omega_ref = 1 rad/s, no disturbance.",
            reference=_step_ref,
        ),
        Scenario(
            name="load_disturbance",
            description=f"Step reference then load torque {LOAD_TORQUE_NM} N·m at t = {LOAD_ONSET_S} s.",
            t_final=4.0,
            reference=_step_ref,
            load_torque=lambda t: _load_step(t),
            disturbance_onset_s=LOAD_ONSET_S,
        ),
        Scenario(
            name="plant_mismatch",
            description="Step reference on mismatched plant (J×1.3, b×0.7, R×1.2).",
            reference=_step_ref,
            plant_scale={"J": 1.3, "b": 0.7, "R": 1.2},
        ),
        Scenario(
            name="noisy_measurement",
            description="Step reference with Gaussian speed sensor noise (σ=0.02).",
            reference=_step_ref,
            noise_std=0.02,
            seed=0,
        ),
    ]


def uncertainty_scenarios() -> list[Scenario]:
    """Phase 6 stress suite: combined mismatch+load and noise intensity sweep."""
    return [
        Scenario(
            name="mismatch_load",
            description=(
                f"Mismatch (J×1.3, b×0.7, R×1.2) + load {LOAD_TORQUE_NM} N·m at t={LOAD_ONSET_S} s."
            ),
            t_final=4.0,
            reference=_step_ref,
            load_torque=lambda t: _load_step(t),
            plant_scale={"J": 1.3, "b": 0.7, "R": 1.2},
            disturbance_onset_s=LOAD_ONSET_S,
        ),
        Scenario(
            name="noise_low",
            description="Step reference with low sensor noise (σ=0.01).",
            reference=_step_ref,
            noise_std=0.01,
            seed=0,
        ),
        Scenario(
            name="noise_med",
            description="Step reference with medium sensor noise (σ=0.03).",
            reference=_step_ref,
            noise_std=0.03,
            seed=0,
        ),
        Scenario(
            name="noise_high",
            description="Step reference with high sensor noise (σ=0.05).",
            reference=_step_ref,
            noise_std=0.05,
            seed=0,
        ),
        Scenario(
            name="mismatch_harsh",
            description="Harsher mismatch (J×1.6, b×0.5, R×1.4).",
            reference=_step_ref,
            plant_scale={"J": 1.6, "b": 0.5, "R": 1.4},
        ),
    ]


def scenario_catalog() -> dict[str, Scenario]:
    """All named scenarios (baseline + uncertainty)."""
    catalog: dict[str, Scenario] = {}
    for s in default_scenarios() + uncertainty_scenarios():
        catalog[s.name] = s
    return catalog


def default_scenarios_extended() -> list[Scenario]:
    """Baseline + Phase 6 uncertainty scenarios (full digital-twin stress suite)."""
    return default_scenarios() + uncertainty_scenarios()


def scenarios_by_names(names: list[str]) -> list[Scenario]:
    """Select scenarios from the full catalog by name (stable order of `names`)."""
    catalog = scenario_catalog()
    missing = [n for n in names if n not in catalog]
    if missing:
        raise KeyError(f"Unknown scenario name(s): {missing}")
    return [catalog[n] for n in names]


def scenarios_from_spec(spec) -> list[Scenario]:
    """Build evaluation scenarios from a DesignSpec (omega_ref / t_final applied).

    Plant physics stay CTMS defaults; only reference level and horizon adapt.
    Load timing / disturbance_onset_s are preserved from the catalog.
    """
    omega_ref = float(getattr(spec, "omega_ref", 1.0))
    t_final_spec = float(getattr(spec, "t_final", 3.0))
    names = list(getattr(spec, "required_scenarios", []) or ["step_1rads"])

    adapted: list[Scenario] = []
    for base in scenarios_by_names(names):
        t_final = max(base.t_final, t_final_spec)
        onset = base.disturbance_onset_s
        load_fn = base.load_torque
        if onset is not None:
            tau = float(base.load_torque(onset))
            load_fn = lambda t, _tau=tau, _onset=onset: _tau if t >= _onset else 0.0

        adapted.append(
            Scenario(
                name=base.name,
                description=base.description,
                t_final=t_final,
                dt=base.dt,
                reference=lambda t, _r=omega_ref: _r,
                load_torque=load_fn,
                plant_scale=dict(base.plant_scale),
                noise_std=base.noise_std,
                seed=base.seed,
                disturbance_onset_s=onset,
            )
        )
    return adapted
