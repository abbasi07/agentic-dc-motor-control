"""Spec validation / DesignSpec helpers (no OpenAI)."""

from __future__ import annotations

from dc_motor.specs import DesignSpec, design_spec_from_dict, validate_and_clamp_design_spec


def test_design_spec_from_dict_and_clamp():
    data = {
        "hard_constraints": {
            "settling_time_s": {"op": "<=", "limit": 1.2},
            "overshoot_pct": {"op": "<=", "limit": 8.0},
        },
        "soft_preferences": {"ITAE": 1.0},
        "required_scenarios": ["step_1rads"],
        "V_max": 12.0,
    }
    spec = design_spec_from_dict(data, raw_spec="from dict", source="manual")
    assert spec.hard_constraints["settling_time_s"][1] == 1.2
    assert "step_1rads" in spec.required_scenarios
    clamped = validate_and_clamp_design_spec(spec)
    assert clamped.hard_constraints


def test_empty_constraints_get_defaults():
    spec = validate_and_clamp_design_spec(DesignSpec(raw_spec="empty", source="manual"))
    assert spec.hard_constraints
    assert "settling_time_s" in spec.hard_constraints
