"""Human-facing labels and formatters for the Design Copilot UI."""

from __future__ import annotations

from typing import Any

# Sidebar / setup
DESIGN_STRATEGY = {
    "heuristic": {
        "label": "Guided redesign (recommended)",
        "help": (
            "After a failed test, automatically try sensible next steps "
            "(retune, relax an infeasible settling demand, try a robust controller, …)."
        ),
    },
    "script": {
        "label": "Single tuning pass",
        "help": "Tune the controller once and stop. Good as a simple baseline.",
    },
    "llm": {
        "label": "AI chooses next steps",
        "help": (
            "Uses the language model to pick the next redesign action. "
            "Requires OPENAI_API_KEY. Metrics still come from simulation only."
        ),
    },
}

JOB_STATUS_LABEL = {
    "draft": "Waiting for your performance goals",
    "needs_clarification": "A few details need clarifying",
    "spec_ready": "Requirements ready — start design",
    "running": "Designing the controller…",
    "completed": "Design finished",
    "failed": "Design could not finish",
    "exported": "Certification package exported",
}

SESSION_OUTCOME = {
    "passed": "All required tests passed.",
    "stopped": "Stopped before meeting every requirement.",
    "budget_exhausted": "Used all redesign attempts without meeting every requirement.",
    "failed": "The design loop failed.",
}

METRIC_LABEL = {
    "rise_time_s": "Rise time",
    "settling_time_s": "Settling time",
    "overshoot_pct": "Overshoot",
    "steady_state_error": "Steady-state error",
    "IAE": "IAE (integral absolute error)",
    "ISE": "ISE (integral square error)",
    "ITAE": "ITAE (time-weighted error)",
    "control_effort": "Control effort",
    "saturation_time_s": "Time in voltage saturation",
    "recovery_time_s": "Recovery time after disturbance",
}

METRIC_UNIT = {
    "rise_time_s": "s",
    "settling_time_s": "s",
    "overshoot_pct": "%",
    "steady_state_error": "",
    "IAE": "",
    "ISE": "",
    "ITAE": "",
    "control_effort": "",
    "saturation_time_s": "s",
    "recovery_time_s": "s",
}

SCENARIO_LABEL = {
    "step_1rads": "Step response (reference = 1 rad/s)",
    "load_disturbance": "Load disturbance",
    "plant_mismatch": "Plant parameter mismatch",
    "noisy_measurement": "Noisy measurement",
    "mismatch_load": "Mismatch + load disturbance",
    "noise_low": "Low measurement noise",
    "noise_med": "Medium measurement noise",
    "noise_high": "High measurement noise",
    "mismatch_harsh": "Harsh plant mismatch",
}

CONTROLLER_LABEL = {
    "pid": "PID speed controller",
    "robust_pid": "Robust PID",
    "lqr": "LQR (optimal state feedback)",
    "lqg": "LQG (LQR + Kalman filter)",
    "mpc": "Model predictive control (MPC)",
    "mrac": "Adaptive control (MRAC)",
    "fuzzy_pid": "Fuzzy PID",
    "adaptive_pid": "Adaptive PID",
    "plant_id": "Plant identification (interim PID)",
}

FEEDBACK_ACTION_LABEL = {
    "accept": "Keep this design",
    "rerun": "Run the design loop again",
    "relax_settling": "Relax the settling-time requirement",
    "expand_scenarios": "Add tougher test scenarios",
    "add_disturbance": "Add a load-disturbance test",
    "tune_again": "Retune the controller gains",
    "call_robust": "Try a robust controller",
    "call_lqr": "Try an LQR (optimal state feedback)",
    "call_lqg": "Try an LQG (optimal + Kalman filter)",
    "call_mpc": "Try MPC",
    "call_mrac": "Try adaptive control (MRAC)",
    "call_fuzzy": "Try a fuzzy PID",
    "call_rl": "Try an adaptive controller",
    "reinterpret_spec": "Re-read your requirements",
    "unclear": "Could not understand the request",
}


def metric_name(key: str) -> str:
    return METRIC_LABEL.get(key, key.replace("_", " "))


def scenario_name(key: str) -> str:
    return SCENARIO_LABEL.get(key, key.replace("_", " "))


def controller_name(kind: str | None) -> str:
    if not kind:
        return "Unknown controller"
    return CONTROLLER_LABEL.get(kind, kind.replace("_", " ").title())


def job_status_label(status: str) -> str:
    return JOB_STATUS_LABEL.get(status, status.replace("_", " "))


def session_outcome(status: str | None) -> str:
    if not status:
        return "No design outcome yet."
    return SESSION_OUTCOME.get(status, status.replace("_", " "))


def format_constraint(metric: str, op: str, limit: float) -> str:
    unit = METRIC_UNIT.get(metric, "")
    lim = f"{limit:g}{(' ' + unit) if unit else ''}"
    return f"{metric_name(metric)} {op} {lim}"


def constraints_as_lines(spec_dict: dict[str, Any] | None) -> list[str]:
    if not spec_dict:
        return []
    lines: list[str] = []
    hard = spec_dict.get("hard_constraints") or {}
    for metric, body in hard.items():
        if isinstance(body, dict):
            lines.append(format_constraint(metric, str(body.get("op", "<=")), float(body["limit"])))
        elif isinstance(body, (list, tuple)) and len(body) == 2:
            lines.append(format_constraint(metric, str(body[0]), float(body[1])))
    return lines


def scenarios_as_lines(spec_dict: dict[str, Any] | None) -> list[str]:
    if not spec_dict:
        return []
    return [scenario_name(s) for s in (spec_dict.get("required_scenarios") or [])]


def limits_summary(spec_dict: dict[str, Any] | None) -> dict[str, str]:
    if not spec_dict:
        return {}
    return {
        "Speed reference": f"{spec_dict.get('omega_ref', 1.0):g} rad/s",
        "Actuator voltage limits": (
            f"{spec_dict.get('V_min', -12):g} … {spec_dict.get('V_max', 12):g} V"
        ),
        "Simulation horizon": f"{spec_dict.get('t_final', 3):g} s",
    }


def feedback_plan_message(plan: dict[str, Any]) -> str:
    action = plan.get("action", "rerun")
    if action == "tune_again":
        return "Understood — I'll retune the controller gains and run the tests again."
    if action == "relax_settling":
        return "I'll relax the settling-time requirement slightly and redesign."
    if action == "call_robust":
        return "I'll try a more robust controller aimed at plant uncertainty."
    if action == "call_lqr":
        return "I'll try an LQR (optimal state feedback with integral action) for tighter tracking."
    if action == "call_lqg":
        return "I'll try an LQG (LQR plus a Kalman filter) to stay robust under measurement noise."
    if action == "call_mpc":
        return "I'll try model predictive control to respect voltage limits more carefully."
    if action in ("call_mrac", "call_rl"):
        return "I'll try an adaptive controller (MRAC) that tunes itself online for changing conditions."
    if action == "call_fuzzy":
        return "I'll try a fuzzy PID that schedules its gains by tracking-error magnitude."
    if action == "expand_scenarios":
        return "I'll add tougher simulation tests and redesign against them."
    if action == "add_disturbance":
        return (
            "I'll add a **load-disturbance** test to the requirements and redesign "
            "so the controller has to reject it."
        )
    if action == "reinterpret_spec":
        return "I'll re-read your requirements and update the performance targets."
    if action == "accept":
        return "Great — this design is marked as accepted. You can download the certification package."
    if action == "rerun":
        return "I'll run the design loop again with your current requirements."
    if action == "unclear":
        return (
            "Sorry, I couldn't map that to a change I can make. Try one of the quick "
            "buttons, or phrase it like “relax settling to 2.5 s”, “add a load disturbance”, "
            "“reduce overshoot”, or “make it more robust”."
        )
    label = FEEDBACK_ACTION_LABEL.get(action, action.replace("_", " "))
    return f"Next step: {label}."


def design_finished_message(session_status: str, kind: str | None, all_pass: bool) -> str:
    ctrl = controller_name(kind)
    if all_pass:
        return (
            f"Design complete. The {ctrl} met all required performance tests. "
            f"{session_outcome(session_status)}"
        )
    return (
        f"Design finished, but the best {ctrl} did **not** meet every requirement. "
        f"{session_outcome(session_status)} "
        "Review the test results below, then either relax a requirement or ask for another redesign."
    )


def scorecard_rows(scorecard: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not scorecard:
        return []
    rows: list[dict[str, Any]] = []
    for item in scorecard.get("scenarios", []):
        rows.append(
            {
                "Test": scenario_name(item.get("name", "")),
                "Result": "Pass" if item.get("constraints", {}).get("all_pass") else "Fail",
                "Score (lower better)": round(float(item.get("scalar_score", 0.0)), 4),
            }
        )
    return rows
