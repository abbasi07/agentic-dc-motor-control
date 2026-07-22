"""Multi-plant registry: PlantSpec catalog + plant factories for evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .plant import CTMS_PARAMS, DCMotorPlant, MotorParams


class PlantProtocol(Protocol):
    """Minimal plant interface used by the evaluation harness."""

    @property
    def omega(self) -> float: ...

    def reset(self) -> None: ...

    def step(self, u: float, dt: float, load_torque: float = 0.0) -> float: ...

    def with_mismatch(self, **scale_or_value: float) -> "PlantProtocol": ...


PlantFactory = Callable[[], PlantProtocol]


@dataclass(frozen=True)
class PlantSpec:
    """Catalog entry for a simulatable plant template."""

    plant_id: str
    name: str
    description: str
    kind: str  # dc_motor | first_order | position_servo
    params: dict[str, float]
    param_units: dict[str, str] = field(default_factory=dict)
    output_name: str = "omega"
    output_unit: str = "rad/s"
    input_name: str = "u"
    input_unit: str = "V"
    V_min: float = -12.0
    V_max: float = 12.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plant_id": self.plant_id,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "params": dict(self.params),
            "param_units": dict(self.param_units),
            "output_name": self.output_name,
            "output_unit": self.output_unit,
            "input_name": self.input_name,
            "input_unit": self.input_unit,
            "V_min": self.V_min,
            "V_max": self.V_max,
        }


class FirstOrderPlant:
    """First-order lag: tau * dy/dt + y = K * u (+ load as output disturbance)."""

    def __init__(self, *, K: float = 1.0, tau: float = 0.5):
        self.K = float(K)
        self.tau = float(tau)
        self.reset()

    def reset(self) -> None:
        self._y = 0.0

    @property
    def omega(self) -> float:
        return self._y

    def step(self, u: float, dt: float, load_torque: float = 0.0) -> float:
        # load_torque reused as additive output disturbance for scenario compatibility
        dy = (-self._y + self.K * u) / max(self.tau, 1e-9)
        self._y = self._y + dy * dt + float(load_torque) * dt
        return self._y

    def with_mismatch(self, **scale_or_value: float) -> "FirstOrderPlant":
        K, tau = self.K, self.tau
        # Accept native keys and CTMS-like aliases used by shared scenarios
        for key, factor in scale_or_value.items():
            f = float(factor)
            if key in ("K", "R"):
                K *= f if key == "K" else (1.0 / f if f else 1.0)
            elif key in ("tau", "J", "L"):
                tau *= f
            elif key == "b":
                tau *= f
        return FirstOrderPlant(K=K, tau=tau)


class PositionServoPlant:
    """Simple rotary inertia plant: J*ddtheta + b*dtheta = u - load; output = position."""

    def __init__(self, *, J: float = 0.02, b: float = 0.15):
        self.J = float(J)
        self.b = float(b)
        self.reset()

    def reset(self) -> None:
        self._theta = 0.0
        self._omega = 0.0

    @property
    def omega(self) -> float:
        # Evaluation harness reads .omega as the controlled output
        return self._theta

    def step(self, u: float, dt: float, load_torque: float = 0.0) -> float:
        domega = (u - self.b * self._omega - float(load_torque)) / max(self.J, 1e-9)
        self._omega = self._omega + domega * dt
        self._theta = self._theta + self._omega * dt
        return self._theta

    def with_mismatch(self, **scale_or_value: float) -> "PositionServoPlant":
        J, b = self.J, self.b
        for key, factor in scale_or_value.items():
            f = float(factor)
            if key == "J":
                J *= f
            elif key in ("b", "R"):
                b *= f
            elif key in ("K", "L"):
                continue
        return PositionServoPlant(J=J, b=b)


def _ctms_factory() -> DCMotorPlant:
    return DCMotorPlant(CTMS_PARAMS)


def _first_order_factory() -> FirstOrderPlant:
    return FirstOrderPlant(K=1.0, tau=0.5)


def _position_factory() -> PositionServoPlant:
    return PositionServoPlant(J=0.02, b=0.15)


_REGISTRY: dict[str, tuple[PlantSpec, PlantFactory]] = {
    "dc_motor_ctms": (
        PlantSpec(
            plant_id="dc_motor_ctms",
            name="CTMS DC Motor (speed)",
            description=(
                "University of Michigan CTMS armature-controlled DC motor speed plant "
                "(J,b,K,R,L)."
            ),
            kind="dc_motor",
            params={
                "J": CTMS_PARAMS.J,
                "b": CTMS_PARAMS.b,
                "K": CTMS_PARAMS.K,
                "R": CTMS_PARAMS.R,
                "L": CTMS_PARAMS.L,
            },
            param_units={
                "J": "kg·m^2",
                "b": "N·m·s/rad",
                "K": "N·m/A",
                "R": "ohm",
                "L": "H",
            },
            output_name="omega",
            output_unit="rad/s",
        ),
        _ctms_factory,
    ),
    "first_order_lag": (
        PlantSpec(
            plant_id="first_order_lag",
            name="First-order lag",
            description="Educational template: tau*dy/dt + y = K*u (output disturbance via load).",
            kind="first_order",
            params={"K": 1.0, "tau": 0.5},
            param_units={"K": "unit/unit", "tau": "s"},
            output_name="y",
            output_unit="unit",
            input_unit="unit",
        ),
        _first_order_factory,
    ),
    "position_servo": (
        PlantSpec(
            plant_id="position_servo",
            name="Simple position servo",
            description="Educational J-b inertia: control position theta with torque-like input u.",
            kind="position_servo",
            params={"J": 0.02, "b": 0.15},
            param_units={"J": "kg·m^2", "b": "N·m·s/rad"},
            output_name="theta",
            output_unit="rad",
            input_unit="N·m",
        ),
        _position_factory,
    ),
}


DEFAULT_PLANT_ID = "dc_motor_ctms"


def list_plants() -> list[PlantSpec]:
    return [entry[0] for entry in _REGISTRY.values()]


def get_plant_spec(plant_id: str) -> PlantSpec:
    if plant_id not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown plant_id={plant_id!r}. Known: {known}")
    return _REGISTRY[plant_id][0]


def get_plant_factory(plant_id: str) -> PlantFactory:
    if plant_id not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown plant_id={plant_id!r}. Known: {known}")
    return _REGISTRY[plant_id][1]


def motor_params_for(plant_id: str) -> MotorParams:
    """Return MotorParams for DC-motor plants; CTMS fallback for educational non-DC kinds.

    Specialists that still assume armature DC dynamics use this. Evaluation uses
    ``get_plant_factory`` so non-DC plants are simulated correctly.
    """
    spec = get_plant_spec(plant_id)
    if spec.kind == "dc_motor":
        p = spec.params
        return MotorParams(J=p["J"], b=p["b"], K=p["K"], R=p["R"], L=p["L"])
    return CTMS_PARAMS


__all__ = [
    "DEFAULT_PLANT_ID",
    "FirstOrderPlant",
    "PlantFactory",
    "PlantProtocol",
    "PlantSpec",
    "PositionServoPlant",
    "get_plant_factory",
    "get_plant_spec",
    "list_plants",
    "motor_params_for",
]
