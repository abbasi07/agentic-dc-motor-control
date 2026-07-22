"""Orchestrator wiring for the new controller-family actions (no OpenAI).

Exercises the deterministic action executor + heuristic/critic policy directly,
without the LLM. Spec is built in-process (no interpret_spec / OpenAI needed).
"""

from __future__ import annotations

import pytest

from agents.critic import diagnose
from agents.orchestrator import (
    AVAILABLE_ACTIONS,
    ActionPlan,
    ORCH_LAB_GRID,
    _execute_action,
    _SessionState,
    heuristic_choose_action,
)
from dc_motor.plant import CTMS_PARAMS
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


def _spec(scenarios: list[str] | None = None) -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="x",
            hard_constraints={
                "settling_time_s": ("<=", 2.0),
                "overshoot_pct": ("<=", 15.0),
                "steady_state_error": ("<=", 0.05),
            },
            soft_preferences={"ITAE": 1.0},
            required_scenarios=scenarios or ["step_1rads"],
            source="manual",
        )
    )


def _run(action: str) -> _SessionState:
    state = _SessionState(spec=_spec())
    state.iteration = 1
    state, record = _execute_action(
        ActionPlan(action, f"test {action}"),
        state,
        grid=ORCH_LAB_GRID,
        maxiter=4,
        seed=0,
        base_params=CTMS_PARAMS,
        plant_factory=None,
    )
    assert record.action == action
    return state


NEW_ACTIONS = ["call_lqr", "call_lqg", "call_mpc", "call_mrac", "call_fuzzy"]
EXPECTED_KIND = {
    "call_lqr": "lqr",
    "call_lqg": "lqg",
    "call_mpc": "mpc",
    "call_mrac": "mrac",
    "call_fuzzy": "fuzzy_pid",
}


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_new_actions_are_available(action: str):
    assert action in AVAILABLE_ACTIONS


@pytest.mark.parametrize("action", NEW_ACTIONS)
def test_new_actions_execute_and_produce_candidate(action: str):
    state = _run(action)
    assert state.best is not None
    assert state.best.kind == EXPECTED_KIND[action]
    assert state.best.scorecard["summary"]["n_scenarios"] == 1


def test_call_rl_is_legacy_alias_for_mrac():
    state = _run("call_rl")
    assert state.best is not None
    assert state.best.kind == "mrac"


def test_heuristic_recommends_family_for_noise_failure():
    # Design a fragile controller, then check the critic/heuristic steer toward a
    # controller family that addresses the resulting failure tags.
    state = _SessionState(spec=_spec(["step_1rads", "noisy_measurement", "plant_mismatch"]))
    state.iteration = 1
    # First heuristic action is always the PID auto-tune.
    plan = heuristic_choose_action(state)
    assert plan.action == "tune_pid_auto"
    state, _ = _execute_action(
        plan, state, grid=ORCH_LAB_GRID, maxiter=4, seed=0, base_params=CTMS_PARAMS
    )
    # If the PID did not pass every stress scenario, the next action must be one of
    # the recommended actions from the grounded diagnosis.
    if state.best is not None and not state.best.failure_digest.all_pass:
        diag = diagnose(state.best, state.spec, tried_actions=tuple(state.actions_tried))
        nxt = heuristic_choose_action(state)
        assert nxt.action in diag.recommended_actions or nxt.action == "tune_pid_scipy"


def test_diagnose_never_recommends_stop_as_action():
    state = _run("call_mpc")
    diag = diagnose(state.best, state.spec, tried_actions=())
    assert "stop" not in diag.recommended_actions
    assert diag.grounded_summary  # non-empty, sourced from FailureDigest
