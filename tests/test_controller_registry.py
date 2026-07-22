"""Tests for the pluggable controller registry (no OpenAI)."""

from __future__ import annotations

import json

import pytest

from agents.controller_registry import (
    CONTROLLER_FAMILIES,
    CONTROLLER_TYPE_NAMES,
    SPECIALIST_ACTIONS,
    design_by_type,
    families_for_tags,
    get_family_by_action,
    get_family_by_type,
    registry_metadata,
)
from agents.orchestrator import AVAILABLE_ACTIONS
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


def _spec() -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="x",
            hard_constraints={"settling_time_s": ("<=", 2.0), "overshoot_pct": ("<=", 15.0)},
            required_scenarios=["step_1rads"],
            source="manual",
        )
    )


def test_controller_type_names_cover_all_families_plus_auto():
    assert CONTROLLER_TYPE_NAMES[0] == "auto"
    for fam in CONTROLLER_FAMILIES:
        assert fam.type_name in CONTROLLER_TYPE_NAMES
    for expected in ("pid", "robust", "lqr", "lqg", "mpc", "mrac", "fuzzy", "adaptive"):
        assert expected in CONTROLLER_TYPE_NAMES


def test_every_family_action_is_a_known_orchestrator_action():
    for fam in CONTROLLER_FAMILIES:
        assert fam.action in AVAILABLE_ACTIONS
    for action in SPECIALIST_ACTIONS:
        assert action.startswith("call_")
        assert get_family_by_action(action) is not None


def test_adaptive_alias_maps_to_mrac():
    fam = get_family_by_type("adaptive")
    assert fam.kind == "mrac"
    assert fam.type_name == "mrac"


def test_design_by_type_dispatches_expected_kinds():
    spec = _spec()
    for type_name, expected_kind in [
        ("pid", "pid"),
        ("robust", "robust_pid"),
        ("lqr", "lqr"),
        ("lqg", "lqg"),
        ("mpc", "mpc"),
        ("mrac", "mrac"),
        ("adaptive", "mrac"),
        ("fuzzy", "fuzzy_pid"),
    ]:
        cand = design_by_type(type_name, spec)
        assert cand.kind == expected_kind


def test_design_by_type_rejects_unknown():
    with pytest.raises(KeyError):
        design_by_type("nope", _spec())


def test_families_for_tags_ranks_by_overlap():
    fams = families_for_tags(["NOISE_SENSITIVE", "MODEL_DISTRUST"])
    kinds = [f.kind for f in fams]
    # LQG explicitly addresses both noise + model distrust, so it should appear.
    assert "lqg" in kinds


def test_registry_metadata_is_json_serializable():
    meta = registry_metadata()
    json.dumps(meta)  # must not raise
    assert {m["type_name"] for m in meta} >= {"pid", "lqr", "lqg", "mpc", "mrac", "fuzzy"}


def test_get_family_by_type_is_case_insensitive():
    assert get_family_by_type("LQR").kind == "lqr"
