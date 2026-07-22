"""Map user feedback text to redesign actions / spec tweaks (heuristic + optional LLM)."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal

from dotenv import load_dotenv

from agents.orchestrator import AVAILABLE_ACTIONS, expand_scenarios, relax_settling_for_load
from agents.spec_agent import DEFAULT_MODEL, _require_api_key
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec

load_dotenv()

FeedbackAction = Literal[
    "accept",
    "rerun",
    "relax_settling",
    "expand_scenarios",
    "add_disturbance",
    "tune_again",
    "call_robust",
    "call_mpc",
    "call_rl",
    "reinterpret_spec",
    "unclear",
]


def heuristic_feedback_plan(text: str) -> dict[str, Any]:
    """Deterministic mapping from feedback phrases to a plan."""
    t = text.lower().strip()
    if not t:
        return {"action": "rerun", "reason": "Empty feedback; rerun design.", "params": {}}

    if any(w in t for w in ("accept", "looks good", "approve", "satisfied", "ship it", "done")):
        return {"action": "accept", "reason": "User accepted the design.", "params": {}}

    if any(w in t for w in ("relax", "too tight", "infeasible", "settling too fast", "can't settle")):
        m = re.search(r"([\d.]+)\s*s", t)
        lim = float(m.group(1)) if m else 2.5
        return {
            "action": "relax_settling",
            "reason": "User asked to relax settling.",
            "params": {"new_limit": lim},
        }

    # Add a load / torque disturbance test (before the broad "robust" check).
    if any(w in t for w in ("disturbance", "load torque", "torque", "add a load", "add load", "step load")):
        return {
            "action": "add_disturbance",
            "reason": "User asked to add a load-disturbance test.",
            "params": {"scenarios": ["load_disturbance"]},
        }

    if any(w in t for w in ("mismatch", "robust", "uncertainty", "fragile")):
        return {"action": "call_robust", "reason": "User asked for robustness.", "params": {}}

    if any(w in t for w in ("mpc", "constraint", "saturation", "voltage limit")):
        return {"action": "call_mpc", "reason": "User asked for MPC / constraint handling.", "params": {}}

    if any(w in t for w in ("adaptive", "learn", "rl", "varying load")):
        return {"action": "call_rl", "reason": "User asked for adaptive control.", "params": {}}

    if any(w in t for w in ("noise", "noisy", "expand scenario", "more scenarios", "stress", "tougher")):
        return {"action": "expand_scenarios", "reason": "User asked to expand scenarios.", "params": {}}

    if any(w in t for w in ("overshoot", "retune", "tune again", "faster", "slower", "error", "sluggish")):
        return {"action": "tune_again", "reason": "User asked to retune.", "params": {}}

    if any(w in t for w in ("change spec", "new requirement", "reinterpret", "different limit")):
        return {
            "action": "reinterpret_spec",
            "reason": "User wants requirements re-interpreted.",
            "params": {},
        }

    # Do NOT silently rerun: tell the user we didn't understand.
    return {
        "action": "unclear",
        "reason": "Feedback did not match a known change.",
        "params": {},
    }


def llm_feedback_plan(text: str, *, scorecard_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Optional LLM mapping; falls back to heuristic on failure."""
    base = heuristic_feedback_plan(text)
    try:
        key = _require_api_key()
    except RuntimeError:
        return base

    from openai import OpenAI

    client = OpenAI(api_key=key)
    model_name = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    system = (
        "Map user feedback on a simulated controller design to ONE JSON object: "
        '{"action": one of '
        f"{list(AVAILABLE_ACTIONS) + ['accept', 'rerun', 'relax_settling', 'tune_again', 'reinterpret_spec']}"
        ', "reason": "...", "params": {}}. '
        "Use scorecard_summary numbers only if provided — never invent metrics."
    )
    user = json.dumps({"feedback": text, "scorecard_summary": scorecard_summary or {}, "heuristic_guess": base})
    try:
        response = client.chat.completions.create(
            model=model_name,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        action = str(data.get("action", base["action"]))
        return {
            "action": action,
            "reason": str(data.get("reason", base["reason"])),
            "params": dict(data.get("params") or {}),
            "source": "llm",
        }
    except Exception:  # noqa: BLE001
        base["source"] = "heuristic"
        return base


def apply_user_feedback(
    spec: DesignSpec,
    feedback: str,
    *,
    use_llm: bool = False,
    scorecard_summary: dict[str, Any] | None = None,
) -> tuple[DesignSpec, dict[str, Any]]:
    """Return updated DesignSpec (if any) and a feedback plan for the job runner."""
    plan = (
        llm_feedback_plan(feedback, scorecard_summary=scorecard_summary)
        if use_llm
        else heuristic_feedback_plan(feedback)
    )
    plan.setdefault("source", "heuristic")
    updated = spec
    action = plan["action"]

    if action == "relax_settling":
        lim = float(plan.get("params", {}).get("new_limit", 2.5))
        updated = relax_settling_for_load(spec, new_limit=lim)
    elif action == "expand_scenarios":
        updated = expand_scenarios(spec)
    elif action == "add_disturbance":
        add = plan.get("params", {}).get("scenarios") or ["load_disturbance"]
        updated = expand_scenarios(spec, add=add)
    else:
        updated = validate_and_clamp_design_spec(spec)

    # Map UI-level actions onto orchestrator modes/actions for the next design run
    if action in {"tune_again", "rerun"}:
        plan["orchestrator_hint"] = "tune_pid_auto"
    elif action in AVAILABLE_ACTIONS:
        plan["orchestrator_hint"] = action
    elif action == "call_robust":
        plan["orchestrator_hint"] = "call_robust"
    elif action == "call_mpc":
        plan["orchestrator_hint"] = "call_mpc"
    elif action == "call_rl":
        plan["orchestrator_hint"] = "call_rl"

    return updated, plan


__all__ = [
    "apply_user_feedback",
    "heuristic_feedback_plan",
    "llm_feedback_plan",
]
