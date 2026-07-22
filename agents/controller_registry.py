"""Pluggable controller registry: kind -> designer + metadata.

This is the single source of truth that ties a controller *family* to:

  * the ``design_controller(type=...)`` name a user picks in chat,
  * the orchestrator action (``call_lqr`` / ``call_mpc`` / …) the redesign loop can select,
  * a human label + description for the SaaS layer, and
  * the :class:`~dc_motor.failure` tags the family is good at addressing (so the
    critic / heuristic policy can pick the right structure for a failure pattern).

Every designer has the uniform signature

    designer(spec, *, base_params, plant_factory=None) -> DesignCandidate

and every controller it returns honours ``reset()`` / ``step(measurement, reference, dt)``.
Adding a new family = adding one :class:`ControllerFamily` entry here; the design
agent, orchestrator menus, and SaaS labels pick it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from dc_motor.plant import CTMS_PARAMS, MotorParams
from dc_motor.specs import DesignSpec

from .design_candidate import DesignCandidate, candidate_from_tune_result
from .pid_tuner import tune_pid
from .specialists import (
    design_fuzzy,
    design_lqg,
    design_lqr,
    design_mpc,
    design_mrac,
    design_robust_pid,
)

Designer = Callable[..., DesignCandidate]


def _design_pid(
    spec: DesignSpec, *, base_params: MotorParams = CTMS_PARAMS, plant_factory=None
) -> DesignCandidate:
    result = tune_pid(spec, method="auto", base_params=base_params, plant_factory=plant_factory)
    return candidate_from_tune_result(result)


@dataclass(frozen=True)
class ControllerFamily:
    """One pluggable controller family behind the shared reset()/step() interface."""

    kind: str  # DesignCandidate.kind produced by the designer
    type_name: str  # design_controller(type=...) selector
    action: str  # orchestrator action name
    label: str  # human-facing label
    description: str
    designer: Designer
    addresses_tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = field(default_factory=tuple)
    requires: tuple[str, ...] = ()  # third-party libs the designer relies on

    def design(
        self, spec: DesignSpec, *, base_params: MotorParams = CTMS_PARAMS, plant_factory=None
    ) -> DesignCandidate:
        return self.designer(spec, base_params=base_params, plant_factory=plant_factory)


CONTROLLER_FAMILIES: tuple[ControllerFamily, ...] = (
    ControllerFamily(
        kind="pid",
        type_name="pid",
        action="tune_pid_auto",
        label="PID speed controller",
        description="Constraint-aware PID (grid + differential-evolution auto-tune).",
        designer=_design_pid,
        addresses_tags=("TRACKING_SLOW", "OVERSHOOT"),
    ),
    ControllerFamily(
        kind="robust_pid",
        type_name="robust",
        action="call_robust",
        label="Robust PID",
        description="Detuned PID tuned against plant mismatch and noise for stability margin.",
        designer=design_robust_pid,
        addresses_tags=("FRAGILE_TO_MISMATCH", "NOISE_SENSITIVE", "MODEL_DISTRUST"),
    ),
    ControllerFamily(
        kind="lqr",
        type_name="lqr",
        action="call_lqr",
        label="LQR (optimal state feedback)",
        description=(
            "Integral-augmented LQR with a Luenberger observer (python-control); "
            "optimal tracking + disturbance rejection via integral action."
        ),
        designer=design_lqr,
        addresses_tags=("TRACKING_SLOW", "OVERSHOOT", "DISTURBANCE_REJECT_FAIL", "RECOVERY_SLOW"),
        requires=("control",),
    ),
    ControllerFamily(
        kind="lqg",
        type_name="lqg",
        action="call_lqg",
        label="LQG (LQR + Kalman filter)",
        description=(
            "Integral-augmented LQR paired with a steady-state Kalman filter "
            "(python-control) for robust estimation under measurement noise."
        ),
        designer=design_lqg,
        addresses_tags=("NOISE_SENSITIVE", "MODEL_DISTRUST", "DISTURBANCE_REJECT_FAIL"),
        requires=("control",),
    ),
    ControllerFamily(
        kind="mpc",
        type_name="mpc",
        action="call_mpc",
        label="Model predictive control (MPC)",
        description=(
            "Constrained receding-horizon QP (cvxpy/OSQP) with hard voltage bounds "
            "and offset-free disturbance rejection."
        ),
        designer=design_mpc,
        addresses_tags=("SATURATION_HEAVY", "DISTURBANCE_REJECT_FAIL", "OVERSHOOT"),
        requires=("cvxpy",),
    ),
    ControllerFamily(
        kind="mrac",
        type_name="mrac",
        action="call_mrac",
        label="Adaptive control (MRAC)",
        description=(
            "Lyapunov model-reference adaptive control: online gain adaptation "
            "against a first-order reference model (sim-only learning)."
        ),
        designer=design_mrac,
        addresses_tags=("MODEL_DISTRUST", "DISTURBANCE_REJECT_FAIL", "RECOVERY_SLOW", "FRAGILE_TO_MISMATCH"),
        aliases=("adaptive",),
    ),
    ControllerFamily(
        kind="fuzzy_pid",
        type_name="fuzzy",
        action="call_fuzzy",
        label="Fuzzy PID",
        description="Takagi–Sugeno fuzzy gain-scheduling PID (error-magnitude scheduling).",
        designer=design_fuzzy,
        addresses_tags=("OVERSHOOT", "TRACKING_SLOW"),
    ),
)


# ---- Derived lookup tables -------------------------------------------------
_BY_TYPE: dict[str, ControllerFamily] = {}
for _fam in CONTROLLER_FAMILIES:
    _BY_TYPE[_fam.type_name] = _fam
    for _alias in _fam.aliases:
        _BY_TYPE[_alias] = _fam

_BY_ACTION: dict[str, ControllerFamily] = {fam.action: fam for fam in CONTROLLER_FAMILIES}
_BY_KIND: dict[str, ControllerFamily] = {fam.kind: fam for fam in CONTROLLER_FAMILIES}

# design_controller(type=...) options exposed to the chat agent ("auto" first).
CONTROLLER_TYPE_NAMES: tuple[str, ...] = ("auto",) + tuple(
    dict.fromkeys([fam.type_name for fam in CONTROLLER_FAMILIES] + [
        a for fam in CONTROLLER_FAMILIES for a in fam.aliases
    ])
)

# Orchestrator "call_*" actions for the non-PID families (PID uses tune_pid_*).
SPECIALIST_ACTIONS: tuple[str, ...] = tuple(
    fam.action for fam in CONTROLLER_FAMILIES if fam.action.startswith("call_")
)


def get_family_by_type(type_name: str) -> ControllerFamily:
    key = (type_name or "").lower().strip()
    if key not in _BY_TYPE:
        raise KeyError(f"Unknown controller type {type_name!r}. Known: {sorted(_BY_TYPE)}")
    return _BY_TYPE[key]


def get_family_by_action(action: str) -> ControllerFamily | None:
    return _BY_ACTION.get(action)


def get_family_by_kind(kind: str) -> ControllerFamily | None:
    return _BY_KIND.get(kind)


def design_by_type(
    type_name: str,
    spec: DesignSpec,
    *,
    base_params: MotorParams = CTMS_PARAMS,
    plant_factory=None,
) -> DesignCandidate:
    """Dispatch a design to the family selected by ``type_name`` (or alias)."""
    return get_family_by_type(type_name).design(
        spec, base_params=base_params, plant_factory=plant_factory
    )


def families_for_tags(tags: list[str]) -> list[ControllerFamily]:
    """Families whose strengths match the given failure tags (ranked by overlap)."""
    scored: list[tuple[int, ControllerFamily]] = []
    tagset = set(tags)
    for fam in CONTROLLER_FAMILIES:
        overlap = len(tagset & set(fam.addresses_tags))
        if overlap:
            scored.append((overlap, fam))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [fam for _, fam in scored]


def registry_metadata() -> list[dict[str, Any]]:
    """JSON-friendly view of the registry (for API / docs / prompts)."""
    return [
        {
            "kind": fam.kind,
            "type_name": fam.type_name,
            "action": fam.action,
            "label": fam.label,
            "description": fam.description,
            "addresses_tags": list(fam.addresses_tags),
            "aliases": list(fam.aliases),
            "requires": list(fam.requires),
        }
        for fam in CONTROLLER_FAMILIES
    ]


__all__ = [
    "ControllerFamily",
    "CONTROLLER_FAMILIES",
    "CONTROLLER_TYPE_NAMES",
    "SPECIALIST_ACTIONS",
    "get_family_by_type",
    "get_family_by_action",
    "get_family_by_kind",
    "design_by_type",
    "families_for_tags",
    "registry_metadata",
]
