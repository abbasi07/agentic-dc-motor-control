"""Plant registry smoke tests."""

from __future__ import annotations

from dc_motor import (
    DEFAULT_PLANT_ID,
    PIDController,
    evaluate_controller,
    get_plant_factory,
    get_plant_spec,
    list_plants,
    scenarios_from_spec,
)
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec


def test_registry_has_three_plants():
    plants = list_plants()
    ids = {p.plant_id for p in plants}
    assert DEFAULT_PLANT_ID in ids
    assert "first_order_lag" in ids
    assert "position_servo" in ids
    assert len(plants) >= 3


def test_first_order_plant_evaluates():
    factory = get_plant_factory("first_order_lag")
    spec = validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="test",
            hard_constraints={
                "settling_time_s": ("<=", 3.0),
                "overshoot_pct": ("<=", 30.0),
                "steady_state_error": ("<=", 0.1),
            },
            required_scenarios=["step_1rads"],
            source="manual",
        )
    )
    pid = PIDController(Kp=8.0, Ki=4.0, Kd=0.0, name="FO_PID")
    card = evaluate_controller(
        pid,
        scenarios=scenarios_from_spec(spec),
        constraints=spec.constraints_for_evaluator(),
        plant_factory=factory,
    )
    assert card["summary"]["n_scenarios"] == 1
    assert get_plant_spec("first_order_lag").kind == "first_order"
