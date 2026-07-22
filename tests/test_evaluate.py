"""Smoke tests for evaluate_controller (deterministic)."""

from __future__ import annotations

from dc_motor import PIDController, default_scenarios, evaluate_controller, scorecard_to_json


def test_evaluate_controller_baseline_structure():
    pid = PIDController(Kp=100.0, Ki=200.0, Kd=10.0, name="PID_test")
    card = evaluate_controller(pid, scenarios=default_scenarios())
    assert "summary" in card
    assert "scenarios" in card
    assert card["controller"] == "PID_test"
    assert len(card["scenarios"]) >= 1
    summary = card["summary"]
    assert "mean_scalar_score" in summary
    assert "all_constraints_pass" in summary
    # JSON round-trip shape
    raw = scorecard_to_json(card)
    assert '"controller"' in raw


def test_evaluate_respects_design_spec_constraints(step_spec):
    pid = PIDController(Kp=100.0, Ki=200.0, Kd=10.0)
    from dc_motor import scenarios_from_spec

    card = evaluate_controller(
        pid,
        scenarios=scenarios_from_spec(step_spec),
        constraints=step_spec.constraints_for_evaluator(),
        score_weights=step_spec.score_weights_for_evaluator(),
    )
    assert card["summary"]["n_scenarios"] == 1
    assert card["scenarios"][0]["name"] == "step_1rads"
