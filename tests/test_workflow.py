"""Workflow phase machine + reflect-only workspace snapshot tests (no OpenAI)."""

from __future__ import annotations

from agents.workflow import (
    PHASE_CONTROLLER_SELECTION,
    PHASE_DESIGNING,
    PHASE_EXPORTED,
    PHASE_GREETING,
    PHASE_MOTOR_AGREED,
    PHASE_MOTOR_NEGOTIATION,
    PHASE_RESULTS_REVIEW,
    PHASE_SPEC_NEGOTIATION,
    build_workspace,
    compute_phase,
)
from saas.jobs import DesignJob


def _job() -> DesignJob:
    return DesignJob(job_id="test-job")


def test_phase_progression():
    job = _job()
    assert compute_phase(job) == PHASE_GREETING

    job.motor_dict = {"name": "m", "params": {"J": 0.01}}
    assert compute_phase(job) == PHASE_MOTOR_NEGOTIATION

    job.motor_confirmed = True
    assert compute_phase(job) == PHASE_MOTOR_AGREED

    job.spec_dict = {"raw_spec": "x", "hard_constraints": {}}
    assert compute_phase(job) == PHASE_SPEC_NEGOTIATION

    job.spec_confirmed = True
    assert compute_phase(job) == PHASE_CONTROLLER_SELECTION

    job.status = "running"
    assert compute_phase(job) == PHASE_DESIGNING

    job.status = "completed"
    job.scorecard = {"summary": {}, "scenarios": []}
    assert compute_phase(job) == PHASE_RESULTS_REVIEW

    job.export_path = "/tmp/pkg.zip"
    assert compute_phase(job) == PHASE_EXPORTED


def test_workspace_only_includes_present_artifacts():
    job = _job()
    ws = build_workspace(job)
    assert ws["phase"] == PHASE_GREETING
    assert ws["artifacts"] == {}
    assert ws["open_tabs"] == []
    assert "budgets" in ws

    job.motor_dict = {
        "name": "m",
        "params": {"J": 0.01},
        "param_units": {"J": "kg·m^2"},
        "V_max": 12.0,
        "V_min": -12.0,
        "characteristics": {"omega_max_rad_s": 2.3},
        "warnings": [],
    }
    ws = build_workspace(job)
    assert "motor" in ws["artifacts"]
    assert ws["artifacts"]["motor"]["confirmed"] is False
    assert ws["artifacts"]["motor"]["characteristics"]["omega_max_rad_s"] == 2.3
    assert "spec" not in ws["artifacts"]


def test_workspace_exposes_plot_trajectories():
    job = _job()
    job.scorecard = {
        "controller": {"kind": "pid"},
        "summary": {"all_constraints_pass": True},
        "scenarios": [
            {
                "name": "step_1rads",
                "metrics": {"settling_time_s": 1.1},
                "constraints": {"all_pass": True},
                "scalar_score": 0.5,
                "trajectories": {"t": [0.0, 0.1], "omega": [0.0, 0.9], "u": [12.0, 8.0]},
            }
        ],
    }
    ws = build_workspace(job)
    assert "results" in ws["artifacts"]
    assert "plots" in ws["artifacts"]
    series = ws["artifacts"]["plots"]["series"]
    assert series[0]["name"] == "step_1rads"
    assert series[0]["omega"] == [0.0, 0.9]
    # Reflect-only results must not carry the big trajectory arrays.
    assert "trajectories" not in ws["artifacts"]["results"]["scenarios"][0]
