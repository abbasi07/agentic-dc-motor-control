"""Certification gate tests (no OpenAI)."""

from __future__ import annotations

from pathlib import Path

from agents import PIDGains, certify_candidate, certify_scorecard, export_certified_package, tune_pid
from agents.design_candidate import candidate_from_tune_result
from agents.pid_tuner import evaluate_pid_gains
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


def test_certify_scorecard_allow_and_block(step_spec):
    result = tune_pid(
        step_spec,
        method="grid",
        grid={"Kp": [100.0], "Ki": [200.0], "Kd": [10.0]},
    )
    cand = candidate_from_tune_result(result)
    cert = certify_candidate(cand)
    assert cert.allowed is True
    assert "ALLOW" in cert.reason

    tight = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="absurdly tight",
            hard_constraints={
                "settling_time_s": ("<=", 0.05),
                "overshoot_pct": ("<=", 1.0),
                "steady_state_error": ("<=", 0.01),
            },
            required_scenarios=["step_1rads"],
            source="manual",
        )
    )
    fail_card = evaluate_pid_gains(PIDGains(40.0, 50.0, 0.0), tight)
    blocked = certify_scorecard(fail_card, params={"Kp": 40.0}, kind="pid")
    assert blocked.allowed is False
    assert "BLOCK" in blocked.reason


def test_export_certified_package_writes_artifacts(step_spec, tmp_path: Path):
    result = tune_pid(
        step_spec,
        method="grid",
        grid={"Kp": [100.0], "Ki": [200.0], "Kd": [10.0]},
    )
    cand = candidate_from_tune_result(result)
    out = export_certified_package(
        cand,
        rationale="Unit test rationale citing scorecard.",
        out_dir=tmp_path,
        nl_spec=step_spec.raw_spec,
        action_trace=[{"action": "tune_pid_grid", "reason": "test"}],
        package_name="test_pkg",
    )
    assert out.suffix == ".zip"
    assert out.exists()
    pkg = tmp_path / "test_pkg"
    assert (pkg / "controller.json").exists()
    assert (pkg / "scorecard.json").exists()
    assert (pkg / "certification.json").exists()
    assert (pkg / "rationale.md").exists()
