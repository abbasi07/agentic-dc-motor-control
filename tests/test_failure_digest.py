"""FailureDigest tagging smoke tests."""

from __future__ import annotations

from agents import PIDGains, evaluate_pid_gains, grid_search_pid
from dc_motor import failure_digest_from_scorecard
from dc_motor.failure import FAILURE_TAGS, TAG_TO_ACTION_HINTS
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


def test_failure_digest_on_passing_baseline(step_spec):
    card = evaluate_pid_gains(PIDGains(100.0, 200.0, 10.0), step_spec)
    digest = failure_digest_from_scorecard(card)
    assert digest.all_pass is True
    assert digest.n_failures == 0
    assert digest.failed_scenarios == []


def test_possibly_infeasible_settling_with_load():
    """Absolute settling <= 1.2 s with load onset at 1.5 s should flag infeasibility."""
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="tight settle + load",
            hard_constraints={
                "settling_time_s": ("<=", 1.2),
                "overshoot_pct": ("<=", 8.0),
                "steady_state_error": ("<=", 0.05),
            },
            required_scenarios=["step_1rads", "load_disturbance"],
            source="manual",
        )
    )
    lab_grid = {
        "Kp": [60.0, 100.0, 160.0],
        "Ki": [100.0, 200.0, 350.0],
        "Kd": [0.0, 10.0],
    }
    result = grid_search_pid(spec, grid=lab_grid, stop_on_pass=False)
    digest = result.failure_digest
    assert digest.all_pass is False
    assert "POSSIBLY_INFEASIBLE_SPEC" in digest.tags or "DISTURBANCE_REJECT_FAIL" in digest.tags
    for tag in digest.tags:
        assert tag in FAILURE_TAGS
    for hint in digest.action_hints:
        assert any(hint in hints for hints in TAG_TO_ACTION_HINTS.values()) or hint in {
            h for hs in TAG_TO_ACTION_HINTS.values() for h in hs
        }
