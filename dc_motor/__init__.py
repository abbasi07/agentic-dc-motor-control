"""Shared DC motor plant, controllers, and evaluation harness."""

from .plant import CTMS_PARAMS, DCMotorPlant, MotorParams
from .motor_model import (
    MotorModel,
    MotorModelValidationError,
    build_motor_model,
    motor_characteristics,
    validate_motor_params,
)
from .feasibility import (
    FeasibilityIssue,
    FeasibilityReport,
    analyze_feasibility,
    check_feasibility,
    min_time_to_reference,
)
from .controllers import PIDController
from .metrics import step_performance_metrics
from .scenarios import (
    Scenario,
    default_scenarios,
    default_scenarios_extended,
    scenarios_by_names,
    scenarios_from_spec,
    uncertainty_scenarios,
)
from .evaluate import evaluate_controller, evaluate_uncertainty_batch, scorecard_to_json
from .failure import (
    FAILURE_TAGS,
    TAG_TO_ACTION_HINTS,
    FailureDigest,
    FailureItem,
    failure_digest_from_scorecard,
)
from .specs import DesignSpec, parse_spec_template, validate_and_clamp_design_spec
from .registry import (
    DEFAULT_PLANT_ID,
    FirstOrderPlant,
    PlantSpec,
    PositionServoPlant,
    get_plant_factory,
    get_plant_spec,
    list_plants,
    motor_params_for,
)

__all__ = [
    "CTMS_PARAMS",
    "DCMotorPlant",
    "MotorParams",
    "MotorModel",
    "MotorModelValidationError",
    "build_motor_model",
    "motor_characteristics",
    "validate_motor_params",
    "FeasibilityIssue",
    "FeasibilityReport",
    "analyze_feasibility",
    "check_feasibility",
    "min_time_to_reference",
    "PIDController",
    "step_performance_metrics",
    "Scenario",
    "default_scenarios",
    "default_scenarios_extended",
    "uncertainty_scenarios",
    "scenarios_by_names",
    "scenarios_from_spec",
    "evaluate_controller",
    "evaluate_uncertainty_batch",
    "scorecard_to_json",
    "FAILURE_TAGS",
    "TAG_TO_ACTION_HINTS",
    "FailureDigest",
    "FailureItem",
    "failure_digest_from_scorecard",
    "DesignSpec",
    "parse_spec_template",
    "validate_and_clamp_design_spec",
    "DEFAULT_PLANT_ID",
    "FirstOrderPlant",
    "PlantSpec",
    "PositionServoPlant",
    "get_plant_factory",
    "get_plant_spec",
    "list_plants",
    "motor_params_for",
]
