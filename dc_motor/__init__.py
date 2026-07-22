"""Shared DC motor plant, controllers, and evaluation harness."""

from .plant import CTMS_PARAMS, DCMotorPlant, MotorParams
from .controllers import PIDController
from .metrics import step_performance_metrics
from .scenarios import Scenario, default_scenarios
from .evaluate import evaluate_controller, scorecard_to_json

__all__ = [
    "CTMS_PARAMS",
    "DCMotorPlant",
    "MotorParams",
    "PIDController",
    "step_performance_metrics",
    "Scenario",
    "default_scenarios",
    "evaluate_controller",
    "scorecard_to_json",
]
