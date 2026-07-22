"""Shared fixtures: fixed DesignSpecs (no OpenAI)."""

from __future__ import annotations

import pytest

from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


@pytest.fixture
def step_spec() -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="test step only",
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


@pytest.fixture
def load_spec() -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="test step + load",
            hard_constraints={
                "settling_time_s": ("<=", 2.5),
                "overshoot_pct": ("<=", 12.0),
                "steady_state_error": ("<=", 0.05),
            },
            soft_preferences={"ITAE": 1.0},
            required_scenarios=["step_1rads", "load_disturbance"],
            source="manual",
        )
    )
