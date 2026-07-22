"""SaaS clarify / feedback / job helpers (no OpenAI)."""

from __future__ import annotations

from unittest.mock import patch

from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec
from saas.clarify import critique_design_spec, deterministic_questions
from saas.feedback import apply_user_feedback, heuristic_feedback_plan
from saas.jobs import JobStore
from saas import service


def _spec_with_warn() -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="tight+load",
            hard_constraints={
                "settling_time_s": ("<=", 1.2),
                "overshoot_pct": ("<=", 8.0),
                "steady_state_error": ("<=", 0.05),
            },
            required_scenarios=["step_1rads", "load_disturbance"],
            source="manual",
            warnings=["settling may be infeasible with load"],
        )
    )


def test_deterministic_clarifying_questions():
    qs = deterministic_questions(_spec_with_warn())
    assert qs
    assert any("1.5" in q or "load" in q.lower() or "warning" in q.lower() for q in qs)


def test_critique_without_llm():
    out = critique_design_spec(_spec_with_warn(), use_llm=False)
    assert out["needs_clarification"] is True
    assert out["source"] == "deterministic"
    assert out["questions"]


def test_heuristic_feedback_plans():
    assert heuristic_feedback_plan("looks good, accept")["action"] == "accept"
    assert heuristic_feedback_plan("please relax settling")["action"] == "relax_settling"
    assert heuristic_feedback_plan("try a robust design")["action"] == "call_robust"


def test_apply_feedback_relaxes_spec():
    spec = _spec_with_warn()
    updated, plan = apply_user_feedback(spec, "relax settling to 2.5s", use_llm=False)
    assert plan["action"] == "relax_settling"
    assert updated.hard_constraints["settling_time_s"][1] >= 2.5


def test_job_confirm_and_run_mocked_session():
    store = JobStore()
    job = store.create(plant_id="dc_motor_ctms", mode="script")
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="easy",
            hard_constraints={
                "settling_time_s": ("<=", 2.0),
                "overshoot_pct": ("<=", 15.0),
                "steady_state_error": ("<=", 0.05),
            },
            required_scenarios=["step_1rads"],
            source="manual",
        )
    )
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.nl_spec = "easy"
    job.status = "spec_ready"

    with patch("saas.service.run_design_session") as mock_run:
        from agents.orchestrator import DesignSession
        from agents.design_candidate import candidate_from_tune_result
        from agents.pid_tuner import tune_pid

        real = tune_pid(spec, method="grid", grid={"Kp": [100.0], "Ki": [200.0], "Kd": [10.0]})
        cand = candidate_from_tune_result(real)
        mock_run.return_value = DesignSession(
            nl_spec="easy",
            mode="script",
            spec=spec,
            status="passed",
            best=cand,
            rationale="test",
            total_wall_time_s=0.1,
            total_tool_evaluations=1,
            total_tokens=0,
        )
        # Use a temporary store injection via get — service uses global store
        with patch("saas.service.get_job_store", return_value=store):
            service.confirm_and_run(job, max_iterations=1)

    assert job.status == "completed"
    assert job.certification is not None
    assert job.scorecard is not None
