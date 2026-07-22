"""Physics-based feasibility checks (no OpenAI)."""

from __future__ import annotations

from dc_motor.feasibility import analyze_feasibility, check_feasibility, min_time_to_reference
from dc_motor.plant import CTMS_PARAMS
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


def test_reachable_reference_is_feasible():
    # CTMS ceiling at 12 V ~ 1.2 rad/s; target 1.0 is reachable.
    report = analyze_feasibility(CTMS_PARAMS, omega_ref=1.0, V_max=12.0)
    assert report.feasible
    assert not report.errors


def test_unreachable_reference_flagged_error():
    # Target well above the motor ceiling => infeasible.
    report = analyze_feasibility(CTMS_PARAMS, omega_ref=100.0, V_max=12.0)
    assert not report.feasible
    codes = {i.code for i in report.errors}
    assert "REFERENCE_UNREACHABLE" in codes


def test_settling_faster_than_physics_is_infeasible():
    t_min = min_time_to_reference(CTMS_PARAMS, omega_ref=1.0, V_max=12.0)
    # Demand settling far below the physical minimum-time-to-reach.
    report = analyze_feasibility(
        CTMS_PARAMS,
        omega_ref=1.0,
        V_max=12.0,
        settling_limit=("<=", t_min / 10.0),
    )
    assert not report.feasible
    assert any(i.code == "SETTLING_INFEASIBLE" for i in report.errors)


def test_near_ceiling_is_warning_not_error():
    ceiling = 0.01 / (0.1 * 1.0 + 0.01**2) * 12.0
    report = analyze_feasibility(CTMS_PARAMS, omega_ref=0.95 * ceiling, V_max=12.0)
    assert report.feasible  # warning, not error
    assert any(i.code == "REFERENCE_NEAR_CEILING" for i in report.warnings)


def test_settling_before_load_onset_warns():
    report = analyze_feasibility(
        CTMS_PARAMS,
        omega_ref=1.0,
        V_max=12.0,
        settling_limit=("<=", 1.0),
        required_scenarios=["step_1rads", "load_disturbance"],
    )
    assert any(i.code == "SETTLING_BEFORE_LOAD_ONSET" for i in report.warnings)


def test_check_feasibility_from_spec():
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="reachable",
            hard_constraints={"settling_time_s": ("<=", 2.0), "overshoot_pct": ("<=", 15.0)},
            required_scenarios=["step_1rads"],
            omega_ref=1.0,
            source="manual",
        )
    )
    report = check_feasibility(CTMS_PARAMS, spec)
    assert report.feasible
    assert "omega_max_rad_s" in report.characteristics
