"""Deterministic tests for the workstream-C controller families (no OpenAI).

Covers the real LQR/LQG, constrained MPC, MRAC, and fuzzy PID designers plus the
shared state-space realization. Every controller must honour reset()/step() and be
scored by the deterministic evaluation harness.
"""

from __future__ import annotations

import numpy as np
import pytest

from agents.specialists import (
    design_fuzzy,
    design_lqg,
    design_lqr,
    design_mpc,
    design_mrac,
)
from dc_motor.plant import CTMS_PARAMS
from dc_motor.motor_model import motor_characteristics
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec
from dc_motor.state_space import motor_state_space


def _step_spec(omega_ref: float = 1.0) -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="settle under 2s, overshoot under 15%",
            hard_constraints={
                "settling_time_s": ("<=", 2.0),
                "overshoot_pct": ("<=", 15.0),
                "steady_state_error": ("<=", 0.05),
            },
            soft_preferences={"ITAE": 1.0, "control_effort": 0.01},
            required_scenarios=["step_1rads"],
            omega_ref=omega_ref,
            source="manual",
        )
    )


DESIGNERS = {
    "lqr": design_lqr,
    "lqg": design_lqg,
    "mpc": design_mpc,
    "mrac": design_mrac,
    "fuzzy": design_fuzzy,
}
EXPECTED_KIND = {
    "lqr": "lqr",
    "lqg": "lqg",
    "mpc": "mpc",
    "mrac": "mrac",
    "fuzzy": "fuzzy_pid",
}


# --------------------------------------------------------------------------- #
# State-space realization
# --------------------------------------------------------------------------- #
def test_state_space_dc_gain_matches_transfer_function():
    sm = motor_state_space(CTMS_PARAMS)
    assert sm.A.shape == (2, 2)
    assert sm.B.shape == (2, 1)
    # Steady-state gain y/u = -C A^{-1} B should match K/(bR+K^2).
    x_ss = -np.linalg.solve(sm.A, sm.B)  # for u = 1
    y_ss = float((sm.C @ x_ss)[0, 0])
    chars = motor_characteristics(CTMS_PARAMS, V_max=12.0)
    assert y_ss == pytest.approx(chars["dc_gain_rad_s_per_V"], rel=1e-6)


def test_state_space_discretization_shapes():
    sm = motor_state_space(CTMS_PARAMS)
    dsm = sm.discretize(0.01)
    assert dsm.Ad.shape == (2, 2)
    assert dsm.Bd.shape == (2, 1)
    assert dsm.dt == pytest.approx(0.01)


# --------------------------------------------------------------------------- #
# Each family: designs, is scored, honours the interface, passes an easy step
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", list(DESIGNERS))
def test_family_designs_and_scores(name: str):
    cand = DESIGNERS[name](_step_spec())
    assert cand.kind == EXPECTED_KIND[name]
    assert cand.scorecard["summary"]["n_scenarios"] == 1
    # reset()/step() interface is honoured by the controller object.
    ctrl = cand.controller
    ctrl.reset()
    u = ctrl.step(0.0, 1.0, 1e-3)
    assert isinstance(float(u), float)
    assert hasattr(ctrl, "last_saturated")


@pytest.mark.parametrize("name", list(DESIGNERS))
def test_family_passes_easy_step(name: str):
    cand = DESIGNERS[name](_step_spec())
    m = cand.scorecard["scenarios"][0]["metrics"]
    # All families should track a plain step to within the soft settling band.
    assert cand.failure_digest.all_pass, f"{name} failed easy step: {cand.failure_digest.summary}"
    assert m["steady_state_error"] <= 0.05


@pytest.mark.parametrize("name", list(DESIGNERS))
def test_family_respects_voltage_limits(name: str):
    cand = DESIGNERS[name](_step_spec())
    u = np.asarray(cand.scorecard["scenarios"][0]["trajectories"]["u"], dtype=float)
    assert np.all(u <= 12.0 + 1e-6)
    assert np.all(u >= -12.0 - 1e-6)


@pytest.mark.parametrize("name", list(DESIGNERS))
def test_family_is_deterministic(name: str):
    a = DESIGNERS[name](_step_spec())
    b = DESIGNERS[name](_step_spec())
    ma = a.scorecard["scenarios"][0]["metrics"]
    mb = b.scorecard["scenarios"][0]["metrics"]
    assert ma["settling_time_s"] == pytest.approx(mb["settling_time_s"], rel=1e-9, nan_ok=True)
    assert ma["steady_state_error"] == pytest.approx(mb["steady_state_error"], rel=1e-9)


def test_lqr_and_lqg_reject_load_via_integral_action():
    # Integral-augmented optimal control should track a step with ~zero SS error.
    for designer in (design_lqr, design_lqg):
        cand = designer(_step_spec())
        assert cand.scorecard["scenarios"][0]["metrics"]["steady_state_error"] < 1e-2


def test_mpc_enforces_hard_input_bound_with_tight_voltage():
    # A modest voltage budget must never be exceeded by the QP solution.
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="tight voltage",
            hard_constraints={"settling_time_s": ("<=", 2.0)},
            required_scenarios=["step_1rads"],
            omega_ref=1.0,
            V_min=-6.0,
            V_max=6.0,
            source="manual",
        )
    )
    cand = design_mpc(spec)
    u = np.asarray(cand.scorecard["scenarios"][0]["trajectories"]["u"], dtype=float)
    assert np.all(np.abs(u) <= 6.0 + 1e-6)
