"""Workflow phases + reflect-only workspace snapshot for the chat-first copilot.

The copilot is a single conversation that walks through a deterministic sequence of
stages. The *phase* is computed from persisted job state (not invented by the LLM), so
the frontend can render the right panels and the backend can gate transitions.

    greeting
        -> motor_negotiation      (a motor is proposed; realism being sorted out)
        -> motor_agreed           (engineer explicitly confirmed the motor)
        -> spec_negotiation       (specs proposed; feasibility being sorted out)
        -> controller_selection   (specs confirmed; ready to pick a controller family)
        -> designing              (a design run is in progress)
        -> results_review         (a scorecard exists; iterate or accept)
        -> exported               (certification package written)

The **workspace** is a reflect-only projection of the session: it contains exactly the
artifacts that currently exist (so the UI can show panels *as they become relevant*).
Every number in it originates from a deterministic tool — never from the LLM. Chat is
the single source of truth; the workspace only displays what has been agreed/computed.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
PHASE_GREETING = "greeting"
PHASE_MOTOR_NEGOTIATION = "motor_negotiation"
PHASE_MOTOR_AGREED = "motor_agreed"
PHASE_SPEC_NEGOTIATION = "spec_negotiation"
PHASE_CONTROLLER_SELECTION = "controller_selection"
PHASE_DESIGNING = "designing"
PHASE_RESULTS_REVIEW = "results_review"
PHASE_EXPORTED = "exported"

PHASE_ORDER: tuple[str, ...] = (
    PHASE_GREETING,
    PHASE_MOTOR_NEGOTIATION,
    PHASE_MOTOR_AGREED,
    PHASE_SPEC_NEGOTIATION,
    PHASE_CONTROLLER_SELECTION,
    PHASE_DESIGNING,
    PHASE_RESULTS_REVIEW,
    PHASE_EXPORTED,
)

PHASE_LABEL: dict[str, str] = {
    PHASE_GREETING: "Describe your motor",
    PHASE_MOTOR_NEGOTIATION: "Confirming the motor",
    PHASE_MOTOR_AGREED: "Motor set — state your goals",
    PHASE_SPEC_NEGOTIATION: "Confirming the requirements",
    PHASE_CONTROLLER_SELECTION: "Pick a controller",
    PHASE_DESIGNING: "Designing the controller",
    PHASE_RESULTS_REVIEW: "Reviewing results",
    PHASE_EXPORTED: "Certification package exported",
}


def _motor_present(job: Any) -> bool:
    return getattr(job, "motor_dict", None) is not None or getattr(job, "_motor", None) is not None


def _spec_present(job: Any) -> bool:
    return getattr(job, "spec_dict", None) is not None or getattr(job, "_spec", None) is not None


def compute_phase(job: Any) -> str:
    """Derive the current workflow phase from persisted job state (deterministic)."""
    if getattr(job, "export_path", None):
        return PHASE_EXPORTED
    if getattr(job, "scorecard", None):
        return PHASE_RESULTS_REVIEW
    if getattr(job, "status", None) in {"queued", "running"}:
        return PHASE_DESIGNING
    if _spec_present(job):
        if getattr(job, "spec_confirmed", False):
            return PHASE_CONTROLLER_SELECTION
        return PHASE_SPEC_NEGOTIATION
    if _motor_present(job):
        if getattr(job, "motor_confirmed", False):
            return PHASE_MOTOR_AGREED
        return PHASE_MOTOR_NEGOTIATION
    return PHASE_GREETING


# --------------------------------------------------------------------------- #
# Workspace artifacts (reflect-only)
# --------------------------------------------------------------------------- #
def _motor_artifact(job: Any) -> dict[str, Any] | None:
    motor = getattr(job, "motor_dict", None)
    if not motor:
        return None
    return {
        "name": motor.get("name"),
        "source": motor.get("source"),
        "params": motor.get("params"),
        "param_units": motor.get("param_units"),
        "V_max": motor.get("V_max"),
        "V_min": motor.get("V_min"),
        "characteristics": motor.get("characteristics"),
        "warnings": motor.get("warnings", []),
        "confirmed": bool(getattr(job, "motor_confirmed", False)),
    }


def _spec_artifact(job: Any) -> dict[str, Any] | None:
    spec = getattr(job, "spec_dict", None)
    if not spec:
        return None
    return {
        "raw_spec": spec.get("raw_spec"),
        "hard_constraints": spec.get("hard_constraints"),
        "soft_preferences": spec.get("soft_preferences"),
        "required_scenarios": spec.get("required_scenarios"),
        "omega_ref": spec.get("omega_ref"),
        "V_max": spec.get("V_max"),
        "V_min": spec.get("V_min"),
        "t_final": spec.get("t_final"),
        "warnings": spec.get("warnings", []),
        "confirmed": bool(getattr(job, "spec_confirmed", False)),
    }


def _results_artifact(job: Any) -> dict[str, Any] | None:
    sc = getattr(job, "scorecard", None)
    if not sc:
        return None
    scenarios = []
    for item in sc.get("scenarios", []):
        scenarios.append(
            {
                "name": item.get("name"),
                "metrics": item.get("metrics"),
                "constraints": item.get("constraints"),
                "scalar_score": item.get("scalar_score"),
            }
        )
    session = getattr(job, "session_dict", None) or {}
    return {
        "controller": sc.get("controller"),
        "summary": sc.get("summary"),
        "scenarios": scenarios,
        "constraints": sc.get("constraints"),
        "session_status": session.get("status"),
        "action_trace": session.get("action_trace"),
        "rationale": session.get("rationale"),
    }


def _plots_artifact(job: Any) -> dict[str, Any] | None:
    """Raw trajectory series for client-side charting (t, omega, u per scenario)."""
    sc = getattr(job, "scorecard", None)
    if not sc:
        return None
    series: list[dict[str, Any]] = []
    for item in sc.get("scenarios", []):
        tr = item.get("trajectories")
        if not tr:
            continue
        series.append(
            {
                "name": item.get("name"),
                "t": tr.get("t"),
                "omega": tr.get("omega"),
                "u": tr.get("u"),
                "reference": tr.get("reference"),
            }
        )
    if not series:
        return None
    return {"series": series}


def _export_artifact(job: Any) -> dict[str, Any] | None:
    path = getattr(job, "export_path", None)
    if not path:
        return None
    return {"path": str(path), "status": getattr(job, "status", None)}


def budgets(job: Any, *, session: Any = None) -> dict[str, Any]:
    """Token + iteration budget usage (background guardrails, surfaced read-only)."""
    tokens_used = int(getattr(session, "total_tokens", 0) or 0) if session is not None else 0
    return {
        "tokens_used": tokens_used,
        "max_iterations": int(getattr(job, "max_iterations", 0) or 0),
    }


def build_workspace(job: Any, *, session: Any = None) -> dict[str, Any]:
    """Reflect-only projection of the session for the frontend.

    Only artifacts that currently exist are included, so the UI can render panels
    dynamically as the conversation reaches each stage.
    """
    phase = compute_phase(job)
    artifacts: dict[str, Any] = {}
    for key, value in (
        ("motor", _motor_artifact(job)),
        ("spec", _spec_artifact(job)),
        ("feasibility", getattr(job, "feasibility", None)),
        ("results", _results_artifact(job)),
        ("plots", _plots_artifact(job)),
        ("certification", getattr(job, "certification", None)),
        ("export", _export_artifact(job)),
    ):
        if value:
            artifacts[key] = value

    return {
        "job_id": getattr(job, "job_id", None),
        "phase": phase,
        "phase_label": PHASE_LABEL.get(phase, phase),
        "status": getattr(job, "status", None),
        "artifacts": artifacts,
        "open_tabs": list(artifacts.keys()),
        "budgets": budgets(job, session=session),
        "error": getattr(job, "error", None),
    }


__all__ = [
    "PHASE_CONTROLLER_SELECTION",
    "PHASE_DESIGNING",
    "PHASE_EXPORTED",
    "PHASE_GREETING",
    "PHASE_LABEL",
    "PHASE_MOTOR_AGREED",
    "PHASE_MOTOR_NEGOTIATION",
    "PHASE_ORDER",
    "PHASE_RESULTS_REVIEW",
    "PHASE_SPEC_NEGOTIATION",
    "build_workspace",
    "budgets",
    "compute_phase",
]
