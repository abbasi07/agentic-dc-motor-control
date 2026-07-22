"""Orchestrator tests with mocked Spec Interpreter (no OpenAI)."""

from __future__ import annotations

from unittest.mock import patch

from agents.orchestrator import run_design_session
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


def _fixed_spec(_: str, **_kwargs) -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="mocked",
            hard_constraints={
                "settling_time_s": ("<=", 2.0),
                "overshoot_pct": ("<=", 15.0),
                "steady_state_error": ("<=", 0.05),
            },
            soft_preferences={"ITAE": 1.0},
            required_scenarios=["step_1rads"],
            max_design_iterations=3,
            source="manual",
        )
    )


def test_run_design_session_script_mode_mocked_spec():
    with patch("agents.orchestrator.interpret_spec", side_effect=_fixed_spec):
        sess = run_design_session("ignored NL", mode="script", max_iterations=1, maxiter_scipy=4)
    assert sess.mode == "script"
    assert sess.spec.source == "manual"
    assert sess.best is not None
    assert len(sess.action_trace) >= 1
    assert sess.action_trace[0].action.startswith("tune_pid")


def test_run_design_session_heuristic_mode_mocked_spec():
    with patch("agents.orchestrator.interpret_spec", side_effect=_fixed_spec):
        sess = run_design_session("ignored NL", mode="heuristic", max_iterations=3, maxiter_scipy=4)
    assert sess.mode == "heuristic"
    assert sess.best is not None
    assert sess.status in {"passed", "budget_exhausted", "stopped"}


def test_interpret_spec_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    from agents.spec_agent import interpret_spec

    try:
        interpret_spec("settle under 1s")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "OpenAI" in str(exc)
    assert raised is True
