"""Motor model validation + transfer-function characteristics (no OpenAI)."""

from __future__ import annotations

import math

import pytest

from dc_motor.motor_model import (
    MotorModelValidationError,
    build_motor_model,
    motor_characteristics,
    validate_motor_params,
)
from dc_motor.plant import CTMS_PARAMS


def test_build_motor_model_ok():
    m = build_motor_model(J=0.01, b=0.1, K=0.01, R=1.0, L=0.5, V_max=12.0, name="ctms")
    assert m.params.J == 0.01
    assert m.V_max == 12.0
    assert m.V_min == -12.0
    assert m.name == "ctms"
    d = m.to_dict()
    assert "characteristics" in d and d["params"]["R"] == 1.0


@pytest.mark.parametrize("bad", [{"J": 0.0}, {"R": -1.0}, {"K": float("nan")}])
def test_build_motor_model_rejects_nonphysical(bad):
    params = {"J": 0.01, "b": 0.1, "K": 0.01, "R": 1.0, "L": 0.5}
    params.update(bad)
    with pytest.raises(MotorModelValidationError):
        build_motor_model(**params)


def test_validate_returns_warning_for_unusual_range():
    # R far outside typical range -> warning (not an error)
    warns = validate_motor_params(J=0.01, b=0.1, K=0.01, R=5000.0, L=0.5)
    assert any("R=" in w for w in warns)


def test_bad_voltage_rejected():
    with pytest.raises(MotorModelValidationError):
        build_motor_model(J=0.01, b=0.1, K=0.01, R=1.0, L=0.5, V_max=0.0)


def test_characteristics_ctms_values():
    c = motor_characteristics(CTMS_PARAMS, V_max=12.0)
    # dc gain = K/(bR + K^2) = 0.01/(0.1*1 + 0.0001) ~= 0.0999
    assert c["dc_gain_rad_s_per_V"] == pytest.approx(0.01 / (0.1 * 1.0 + 0.01**2), rel=1e-6)
    assert c["omega_max_rad_s"] == pytest.approx(c["dc_gain_rad_s_per_V"] * 12.0, rel=1e-9)
    # tau_mech = J R / (bR + K^2)
    assert c["tau_mech_s"] == pytest.approx(0.01 * 1.0 / (0.1 * 1.0 + 0.01**2), rel=1e-6)
    assert c["tau_elec_s"] == pytest.approx(0.5 / 1.0, rel=1e-9)
    assert c["damping"] in {"overdamped", "critically_damped", "underdamped"}
    assert not math.isnan(c["wn_rad_s"])
