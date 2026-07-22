"""Design Agent (workstream D) tests — fully deterministic, no OpenAI required.

The chat() loop is OpenAI-only, but every *tool* is plain Python and is exercised
here directly. The key guarantee under test: query_results never emits a number that
is absent from the stored scorecard.
"""

from __future__ import annotations

import math
import re

import pytest

from agents.design_agent import DesignAgentSession, scorecard_numbers
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec

_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _easy_step_spec() -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="settle under 2s, overshoot under 15%",
            hard_constraints={
                "settling_time_s": ("<=", 2.0),
                "overshoot_pct": ("<=", 15.0),
                "steady_state_error": ("<=", 0.05),
            },
            soft_preferences={"ITAE": 1.0, "control_effort": 0.05},
            required_scenarios=["step_1rads"],
            source="manual",
        )
    )


def _designed_session() -> DesignAgentSession:
    session = DesignAgentSession.create(plant_id="dc_motor_ctms", mode="heuristic")
    session.load_spec(_easy_step_spec())
    session.design_controller(controller_type="pid")
    return session


def _numbers_in(text: str) -> list[float]:
    return [float(m) for m in _NUMBER_RE.findall(text)]


def test_design_controller_pid_populates_scorecard():
    session = _designed_session()
    job = session.job
    assert job.scorecard is not None
    assert job.certification is not None
    assert job.status == "completed"
    summary = job.scorecard["summary"]
    assert summary["all_constraints_pass"] is True


def test_query_results_before_design_is_safe():
    session = DesignAgentSession.create(plant_id="dc_motor_ctms")
    out = session.query_results("what was the settling time?")
    assert out["grounded"] is True
    assert out["facts"] == []
    assert "no simulation results" in out["answer"].lower()


@pytest.mark.parametrize(
    "question",
    [
        "what was the settling time on the step response?",
        "how much overshoot did we get?",
        "what is the steady-state error?",
        "tell me the ITAE and control effort",
        "did the design pass all requirements?",
        "what was the settling time on every test?",
    ],
)
def test_query_results_emits_no_number_absent_from_scorecard(question: str):
    session = _designed_session()
    allowed = scorecard_numbers(session.job.scorecard)

    out = session.query_results(question)
    assert out["grounded"] is True

    # 1) Every structured fact value must exist in the scorecard.
    for fact in out["facts"]:
        value = fact.get("value")
        if value is None or fact.get("source") != "scorecard":
            continue
        assert any(
            math.isclose(float(value), a, rel_tol=1e-3, abs_tol=1e-3) for a in allowed
        ), f"fact {fact} not grounded in scorecard"

    # 2) Every number in the rendered answer must exist in the scorecard.
    for num in _numbers_in(out["answer"]):
        assert any(
            math.isclose(num, a, rel_tol=1e-3, abs_tol=1e-3) for a in allowed
        ), f"answer emitted ungrounded number {num}: {out['answer']!r}"


def test_query_results_returns_actual_settling_value():
    session = _designed_session()
    item = next(i for i in session.job.scorecard["scenarios"] if i["name"] == "step_1rads")
    true_settling = float(item["metrics"]["settling_time_s"])

    out = session.query_results("what was the settling time on the step response?")
    settling_facts = [f for f in out["facts"] if f.get("metric") == "settling_time_s"]
    assert settling_facts
    assert math.isclose(settling_facts[0]["value"], true_settling, rel_tol=1e-9)


def test_check_feasibility_flags_unreachable_target():
    session = DesignAgentSession.create(plant_id="dc_motor_ctms")
    # CTMS ceiling at 12 V is well below 50 rad/s -> must be infeasible.
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="spin to 50 rad/s",
            hard_constraints={"settling_time_s": ("<=", 2.0)},
            required_scenarios=["step_1rads"],
            omega_ref=50.0,
            source="manual",
        )
    )
    session.load_spec(spec)
    report = session.check_feasibility()
    assert report["feasible"] is False
    assert any(i["severity"] == "error" for i in report["issues"])


def test_modify_relaxes_settling():
    session = _designed_session()
    out = session.modify("relax settling to 2.5 s")
    assert out["action"] == "relax_settling"
    assert session.job._spec.hard_constraints["settling_time_s"][1] >= 2.5


def test_export_gate_allows_certified_design(tmp_path):
    session = _designed_session()
    # Export through the tool (uses the code-enforced certification gate).
    from saas import service

    path = service.export_job(session.job, out_dir=tmp_path)
    assert path.exists()
    assert session.job.certification["allowed"] is True


def test_design_specialist_types_store_scorecard():
    session = DesignAgentSession.create(plant_id="dc_motor_ctms")
    session.load_spec(_easy_step_spec())
    for ctype in ("robust", "mpc", "adaptive"):
        out = session.design_controller(controller_type=ctype)
        assert out["controller_type"] == ctype
        assert session.job.scorecard is not None
        assert out["n_scenarios"] == 1


def test_dispatch_tool_records_log_and_handles_unknown():
    session = _designed_session()
    ok = session._dispatch_tool("query_results", '{"question": "did it pass?"}')
    assert ok["grounded"] is True
    bad = session._dispatch_tool("does_not_exist", "{}")
    assert "error" in bad
    assert session.tool_log[-1]["tool"] == "does_not_exist"
