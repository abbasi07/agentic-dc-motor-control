"""Spec validation / DesignSpec helpers (no OpenAI)."""

from __future__ import annotations

import math

from dc_motor.specs import (
    PROV_DERIVED,
    PROV_LLM,
    PROV_USER,
    DesignSpec,
    apply_plant_voltage_budget,
    apply_spec_edits,
    build_disclosures,
    design_spec_from_dict,
    extract_constraints_from_text,
    extract_omega_ref_from_text,
    extract_t_final_from_text,
    finalize_llm_spec,
    reconcile_scenarios_with_text,
    reconcile_spec_with_plant,
    rpm_to_rad_s,
    spec_sanity_advisories,
    suggest_t_final,
    validate_and_clamp_design_spec,
)


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


def test_rpm_to_rad_s_conversion():
    assert math.isclose(rpm_to_rad_s(60.0), 2.0 * math.pi, rel_tol=1e-12)
    assert math.isclose(rpm_to_rad_s(2800.0), 2800.0 * 2.0 * math.pi / 60.0, rel_tol=1e-12)


def test_extract_omega_ref_from_rpm_text():
    omega = extract_omega_ref_from_text(
        "target speed 2800 RPM, max overshoot 30%, ss error tol 5%, settling time 1 sec"
    )
    assert omega is not None
    assert math.isclose(omega, rpm_to_rad_s(2800.0), rel_tol=1e-9)


def test_design_spec_from_dict_converts_rpm_even_if_llm_left_default():
    # Mimics the Spec LLM leaving omega_ref=1 while the user stated RPM.
    data = {
        "hard_constraints": {
            "settling_time_s": {"op": "<=", "limit": 1.0},
            "overshoot_pct": {"op": "<=", "limit": 30.0},
            "steady_state_error": {"op": "<=", "limit": 0.05},
        },
        "required_scenarios": ["step_1rads"],
        "omega_ref": 1.0,
        "V_min": -12.0,
        "V_max": 12.0,
    }
    raw = "target speed 2800 RPM, max overshoot 30%, ss error tol 5%, settling time 1 sec"
    spec = design_spec_from_dict(data, raw_spec=raw, source="llm")
    assert math.isclose(spec.omega_ref, rpm_to_rad_s(2800.0), rel_tol=1e-6)
    assert any("RPM" in w or "omega_ref" in w for w in spec.warnings)


def test_omega_ref_allows_realistic_motor_speeds():
    # Previously clamped to ≤20 rad/s, which discarded real targets.
    spec = validate_and_clamp_design_spec(
        DesignSpec(raw_spec="spin fast", omega_ref=293.215, source="manual")
    )
    assert math.isclose(spec.omega_ref, 293.215, rel_tol=1e-9)


def test_reconcile_inherits_plant_voltage_budget():
    draft = DesignSpec(
        raw_spec="settle under 1s",
        hard_constraints={"settling_time_s": ("<=", 1.0)},
        omega_ref=1.0,
        V_min=-12.0,
        V_max=12.0,
        source="llm",
    )
    spec = reconcile_spec_with_plant(draft, plant_V_max=20.0)
    assert math.isclose(spec.V_max, 20.0)
    assert math.isclose(spec.V_min, -20.0)
    assert any("20" in w for w in spec.warnings)


def test_apply_plant_voltage_budget_idempotent_when_already_aligned():
    draft = DesignSpec(
        raw_spec="x",
        V_min=-20.0,
        V_max=20.0,
        source="manual",
    )
    updated = apply_plant_voltage_budget(draft, 20.0)
    assert updated.V_max == 20.0
    assert updated.V_min == -20.0
    assert updated.warnings == draft.warnings


# --------------------------------------------------------------------------- #
# Deterministic requirement extraction (the "LLM may not invent numbers" layer)
# --------------------------------------------------------------------------- #


def test_extract_constraints_from_natural_language():
    c = extract_constraints_from_text(
        "target speed 100 rad/s, max overshoot 30%, ss error tol 5%, settling time 35 s"
    )
    assert c["settling_time_s"] == ("<=", 35.0)
    assert c["overshoot_pct"] == ("<=", 30.0)
    assert math.isclose(c["steady_state_error"][1], 0.05, rel_tol=1e-9)


def test_extract_t_final_from_horizon_phrasings():
    assert extract_t_final_from_text("extend simulation horizon to at least 60 s") == 60.0
    assert extract_t_final_from_text("set t_final to 45 seconds") == 45.0
    assert extract_t_final_from_text("just make overshoot smaller") is None


def test_settling_35_is_not_clamped_to_15():
    # Regression: the old (0.05, 15.0) bound silently rewrote 35 s -> 15 s.
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="slow motor",
            hard_constraints={"settling_time_s": ("<=", 35.0)},
            source="manual",
        )
    )
    assert spec.hard_constraints["settling_time_s"][1] == 35.0


def test_horizon_auto_raised_to_cover_settling():
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="slow",
            hard_constraints={"settling_time_s": ("<=", 35.0)},
            t_final=3.0,
            source="manual",
        )
    )
    assert spec.t_final >= 35.0


def test_suggest_t_final_precedence():
    # Explicit user horizon always wins.
    assert suggest_t_final(settling=35.0, explicit=60.0, current=3.0) == 60.0
    # Otherwise derive from settling (1.5x, rounded up), never below current.
    assert suggest_t_final(settling=35.0, explicit=None, current=3.0) == 53.0
    assert suggest_t_final(settling=None, explicit=None, current=10.0) == 10.0


def test_reconcile_scenarios_drops_unrequested():
    # LLM leaked load_disturbance; the user text never mentions load -> dropped.
    kept = reconcile_scenarios_with_text(
        ["step_1rads", "load_disturbance"], "overshoot under 10%, settle in 2 s"
    )
    assert kept == ["step_1rads"]


def test_reconcile_scenarios_keeps_requested():
    kept = reconcile_scenarios_with_text(
        ["step_1rads"], "please add a load disturbance rejection test"
    )
    assert "load_disturbance" in kept


def test_finalize_llm_spec_grounds_numbers_and_scenarios():
    # Mimics a weak model echoing the schema example (wrong numbers + leaked scenario).
    leaked = DesignSpec(
        raw_spec="target speed 100 rad/s, max overshoot 30%, ss error tol 5%, "
        "settling time 35 s, simulation horizon 60 s",
        hard_constraints={
            "settling_time_s": ("<=", 1.2),  # leaked example value
            "overshoot_pct": ("<=", 8.0),  # leaked example value
            "steady_state_error": ("<=", 0.05),
        },
        required_scenarios=["step_1rads", "load_disturbance"],  # leaked scenario
        source="llm",
    )
    spec = finalize_llm_spec(leaked, leaked.raw_spec)
    assert spec.hard_constraints["settling_time_s"][1] == 35.0  # text wins
    assert spec.hard_constraints["overshoot_pct"][1] == 30.0  # text wins
    assert spec.required_scenarios == ["step_1rads"]  # unrequested scenario dropped
    assert spec.t_final == 60.0  # explicit horizon honored


def test_apply_spec_edits_settling_and_horizon():
    base = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="x",
            hard_constraints={"settling_time_s": ("<=", 1.0)},
            t_final=3.0,
            source="manual",
        )
    )
    edited, changes = apply_spec_edits(base, settling=35.0, t_final=60.0)
    assert edited.hard_constraints["settling_time_s"][1] == 35.0
    assert edited.t_final == 60.0
    assert any("settling_time_s" in c for c in changes)
    # An explicit edit is recorded as a user decision.
    assert edited.provenance["settling_time_s"] == PROV_USER


# --------------------------------------------------------------------------- #
# Provenance / disclosure / sanity-advisory model (LLM leads, engine discloses)
# --------------------------------------------------------------------------- #


def test_finalize_keeps_llm_value_but_tags_it_for_disclosure():
    # User stated only overshoot; the interpreter also proposed a settling time.
    llm = DesignSpec(
        raw_spec="keep overshoot under 10%",
        hard_constraints={
            "overshoot_pct": ("<=", 10.0),
            "settling_time_s": ("<=", 2.0),  # NOT in the user's text
        },
        required_scenarios=["step_1rads"],
        source="llm",
    )
    spec = finalize_llm_spec(llm, llm.raw_spec)
    # The interpreter's value is KEPT (agentic) ...
    assert spec.hard_constraints["settling_time_s"][1] == 2.0
    # ... but labelled so it must be disclosed.
    assert spec.provenance["settling_time_s"] == PROV_LLM
    assert spec.provenance["overshoot_pct"] == PROV_USER
    disclosures = build_disclosures(spec)
    assert any("settling time" in d.lower() and "assumed" in d.lower() for d in disclosures)


def test_finalize_corrects_contradicting_number_to_user_text():
    # Interpreter misread the user's stated settling time.
    llm = DesignSpec(
        raw_spec="settling time 35 s please",
        hard_constraints={"settling_time_s": ("<=", 15.0)},
        source="llm",
    )
    spec = finalize_llm_spec(llm, llm.raw_spec)
    assert spec.hard_constraints["settling_time_s"][1] == 35.0
    assert spec.provenance["settling_time_s"] == PROV_USER
    assert any("corrected" in w.lower() for w in spec.warnings)


def test_finalize_holds_back_unrequested_scenario_as_suggestion():
    llm = DesignSpec(
        raw_spec="overshoot under 10%, settle in 2 s",
        hard_constraints={"overshoot_pct": ("<=", 10.0)},
        required_scenarios=["step_1rads", "load_disturbance"],  # leaked
        source="llm",
    )
    spec = finalize_llm_spec(llm, llm.raw_spec)
    assert spec.required_scenarios == ["step_1rads"]  # not silently activated
    assert spec.provenance.get("scenario_suggested:load_disturbance") == PROV_LLM
    assert any("load_disturbance" in d for d in build_disclosures(spec))


def test_derived_horizon_is_disclosed():
    llm = DesignSpec(
        raw_spec="settling time 35 s",
        hard_constraints={"settling_time_s": ("<=", 35.0)},
        source="llm",
    )
    spec = finalize_llm_spec(llm, llm.raw_spec)
    assert spec.provenance["t_final"] == PROV_DERIVED
    assert any("horizon" in d.lower() for d in build_disclosures(spec))


def test_sanity_advisories_flag_unrealistic_values():
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="perfect tracking",
            hard_constraints={
                "steady_state_error": ("<=", 0.0001),  # 0.01% — too tight
                "overshoot_pct": ("<=", 0.0),  # zero overshoot
            },
            source="manual",
        )
    )
    codes = {a["code"] for a in spec_sanity_advisories(spec)}
    assert "SSE_VERY_TIGHT" in codes
    assert "OVERSHOOT_ZERO" in codes
    # Every advisory carries a reason and a concrete suggestion.
    for a in spec_sanity_advisories(spec):
        assert a["message"] and a["suggestion"]


def test_provenance_survives_reconcile_and_roundtrip():
    llm = DesignSpec(
        raw_spec="overshoot under 10%",
        hard_constraints={"overshoot_pct": ("<=", 10.0), "settling_time_s": ("<=", 2.0)},
        source="llm",
    )
    spec = finalize_llm_spec(llm, llm.raw_spec)
    # Reconcile (plant voltage) must not wipe provenance.
    spec2 = reconcile_spec_with_plant(spec, plant_V_max=20.0)
    assert spec2.provenance["settling_time_s"] == PROV_LLM
    # to_dict/from_dict round-trip preserves provenance.
    rehydrated = design_spec_from_dict(spec2.to_dict(), raw_spec=spec2.raw_spec, source="llm")
    assert rehydrated.provenance["settling_time_s"] == PROV_LLM
