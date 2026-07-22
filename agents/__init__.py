"""Agents package: LLM and tool wrappers for adaptive controller design."""

from .spec_agent import interpret_spec, interpret_spec_llm, interpret_spec_auto, llm_unavailable_message
from .plant_agent import interpret_plant, motor_model_from_dict
from .pid_tuner import (
    PIDGains,
    TuneResult,
    evaluate_pid_gains,
    grid_search_pid,
    optimize_pid,
    tune_pid,
    zn_warm_start,
)
from .orchestrator import (
    AVAILABLE_ACTIONS,
    DesignSession,
    run_design_session,
)
from .specialists import (
    AdaptivePIDController,
    MPCController,
    design_adaptive,
    design_mpc,
    design_robust_pid,
    identify_plant_sim,
    run_identify_plant,
)
from .certify import (
    CertificationResult,
    certify_candidate,
    certify_scorecard,
    export_certified_package,
)
from .design_candidate import DesignCandidate
from .design_agent import (
    CONTROLLER_TYPES,
    TOOL_SCHEMAS,
    DesignAgentSession,
    scorecard_numbers,
)

__all__ = [
    "interpret_spec",
    "interpret_spec_llm",
    "interpret_spec_auto",
    "llm_unavailable_message",
    "interpret_plant",
    "motor_model_from_dict",
    "PIDGains",
    "TuneResult",
    "evaluate_pid_gains",
    "grid_search_pid",
    "optimize_pid",
    "tune_pid",
    "zn_warm_start",
    "AVAILABLE_ACTIONS",
    "DesignSession",
    "run_design_session",
    "AdaptivePIDController",
    "MPCController",
    "design_adaptive",
    "design_mpc",
    "design_robust_pid",
    "identify_plant_sim",
    "run_identify_plant",
    "CertificationResult",
    "certify_candidate",
    "certify_scorecard",
    "export_certified_package",
    "DesignCandidate",
    "CONTROLLER_TYPES",
    "TOOL_SCHEMAS",
    "DesignAgentSession",
    "scorecard_numbers",
]
