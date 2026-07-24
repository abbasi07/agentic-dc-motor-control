"""Spec Interpreter Agent: natural-language performance specs -> DesignSpec.

OpenAI-only: there is no regex/template fallback. If the LLM is unavailable,
a clear message is printed and RuntimeError is raised.
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv

from dc_motor.specs import (
    ALLOWED_METRICS,
    ALLOWED_SCENARIOS,
    DesignSpec,
    design_spec_from_dict,
    finalize_llm_spec,
)

load_dotenv()

DEFAULT_MODEL = "gpt-5.4-nano"

# NOTE: the schema hint intentionally contains NO concrete numbers or scenarios.
# Weak models copy example values verbatim (this is exactly how an unrequested
# `load_disturbance` and placeholder limits leaked into specs). Placeholders are
# type descriptors only; the deterministic layer (`finalize_llm_spec`) is what
# actually fixes every number and scenario against the user's own words.
SPEC_JSON_SCHEMA_HINT = {
    "raw_spec": "<echo the user's exact text>",
    "hard_constraints": {
        "<metric_name>": {"op": "<=|>=|<|>", "limit": "<number the USER stated>"}
    },
    "soft_preferences": {"<metric_name>": "<non-negative weight>"},
    "required_scenarios": ["<only scenarios the USER explicitly asked for>"],
    "omega_ref": "<rad/s; convert RPM; null if the user gave no target>",
    "V_min": "<lower voltage limit or null>",
    "V_max": "<upper voltage limit or null>",
    "t_final": "<simulation horizon in seconds, or null>",
    "max_design_iterations": "<integer or null>",
    "stop_on_pass": True,
    "notes": "<short clarification of assumptions>",
}


def llm_unavailable_message(*, detail: str | None = None) -> str:
    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    msg = (
        "OpenAI LLM is required but unavailable. "
        "Copy .env.example to .env and set OPENAI_API_KEY. "
        f"Default model: {model}."
    )
    if detail:
        msg = f"{msg} Detail: {detail}"
    return msg


def _require_api_key(api_key: str | None = None) -> str:
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key or key.startswith("sk-your-key"):
        msg = llm_unavailable_message(detail="OPENAI_API_KEY missing or placeholder.")
        print(msg)
        raise RuntimeError(msg)
    return key


def _system_prompt(*, plant_V_max: float | None = None) -> str:
    voltage_rule = (
        f"- Voltage limits: inherit the plant actuator budget ±{plant_V_max:g} V "
        "(do NOT default to ±12 V when a plant voltage is known)."
        if plant_V_max is not None
        else "- Voltage limits default to ±12 V if unspecified and no plant budget is provided."
    )
    return f"""You are a control-systems Spec Interpreter for DC motor SPEED control simulation.
Convert the user's natural-language requirements into ONE JSON object matching this shape:
{json.dumps(SPEC_JSON_SCHEMA_HINT, indent=2)}

Rules:
- Only use metrics from: {sorted(ALLOWED_METRICS)}
- Only use scenarios from: {sorted(ALLOWED_SCENARIOS)}
- Operators allowed: <=, >=, <, >
- Prefer hard_constraints for must-meet limits; soft_preferences are lower-is-better weights for ranking.

FAITHFUL EXTRACTION (values are provenance-tracked, so honesty matters):
- The placeholder JSON above is a SHAPE ONLY. NEVER copy its example text or numbers.
- Extract every value the user STATED exactly as stated (these become authoritative
  'user' values). Convert units but do not alter magnitudes.
- For a value the user did NOT state, prefer null. You MAY propose a sensible value
  ONLY when the user asked you to choose ("pick a good X", "you decide") — but such
  values are automatically labelled as assumptions and the engineer must approve them,
  so never present a guess as if the user stated it.
- required_scenarios: include ONLY scenarios the user explicitly asked for. Always
  include "step_1rads". Add "load_disturbance" ONLY if the user mentions
  load/disturbance/torque; "plant_mismatch" ONLY for uncertainty/mismatch/parameter
  variation; "noisy_measurement" ONLY for noise/sensor. If unsure, leave it out.
{voltage_rule}
- omega_ref MUST be in rad/s. If the user gives RPM (or rev/min), convert: omega_rad_s = rpm * 2*pi/60.
  (e.g. 2800 RPM → 293.215 rad/s). Never leave omega_ref=1.0 when a target speed was stated.
- Do NOT invent plant physics. Output JSON only (no markdown fences).

Note: a deterministic layer cross-checks your JSON against the user's text, tags each
value's source (user / assumed / default / derived), and corrects any number that
contradicts an explicit user value. Extract faithfully so the labels are accurate.
"""


def interpret_spec(
    text: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    plant_V_max: float | None = None,
) -> DesignSpec:
    """OpenAI Spec Interpreter -> validated DesignSpec (no regex fallback)."""
    return interpret_spec_llm(
        text, model=model, api_key=api_key, plant_V_max=plant_V_max
    )


def interpret_spec_llm(
    text: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    plant_V_max: float | None = None,
) -> DesignSpec:
    """OpenAI Spec Interpreter -> validated DesignSpec."""
    key = _require_api_key(api_key)

    from openai import OpenAI

    client = OpenAI(api_key=key)
    model_name = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

    user_content = text
    if plant_V_max is not None:
        user_content = (
            f"{text}\n\n[Plant context: actuator voltage budget V_max={float(plant_V_max):g} V. "
            "Set V_min/V_max to ± that budget unless the user explicitly requested a tighter limit.]"
        )

    try:
        response = client.chat.completions.create(
            model=model_name,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _system_prompt(plant_V_max=plant_V_max)},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        msg = llm_unavailable_message(detail=str(exc))
        print(msg)
        raise RuntimeError(msg) from exc

    content = response.choices[0].message.content or "{}"
    data: dict[str, Any] = json.loads(content)
    data["raw_spec"] = text
    data = _sanitize_llm_spec_data(data)
    spec = design_spec_from_dict(data, raw_spec=text, source="llm")
    # Deterministic firewall: override the model's numbers/scenarios with what the
    # user's text actually says. This makes the result independent of model quality.
    return finalize_llm_spec(spec, text)


def _coerce_number(value: Any) -> float | None:
    """Return a float only for genuine numbers; None for null/placeholder strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _sanitize_llm_spec_data(data: dict[str, Any]) -> dict[str, Any]:
    """Drop null/placeholder values so a weak model's schema echoes never crash.

    The model is now told to emit ``null`` for anything the user did not specify;
    we strip those so ``design_spec_from_dict`` only ever sees real numbers and the
    dataclass defaults fill the rest. ``finalize_llm_spec`` then grounds everything
    in the user's text.
    """
    clean: dict[str, Any] = {"raw_spec": data.get("raw_spec", "")}

    hc_out: dict[str, Any] = {}
    for metric, body in (data.get("hard_constraints") or {}).items():
        if metric not in ALLOWED_METRICS:
            continue
        limit = None
        op = "<="
        if isinstance(body, dict):
            limit = _coerce_number(body.get("limit"))
            op = str(body.get("op") or "<=")
        elif isinstance(body, (list, tuple)) and len(body) == 2:
            op = str(body[0])
            limit = _coerce_number(body[1])
        if limit is not None:
            hc_out[metric] = {"op": op, "limit": limit}
    clean["hard_constraints"] = hc_out

    prefs: dict[str, float] = {}
    for metric, weight in (data.get("soft_preferences") or {}).items():
        w = _coerce_number(weight)
        if metric in ALLOWED_METRICS and w is not None:
            prefs[metric] = w
    clean["soft_preferences"] = prefs

    clean["required_scenarios"] = [
        s for s in (data.get("required_scenarios") or []) if isinstance(s, str)
    ]

    for key in ("omega_ref", "V_min", "V_max", "t_final"):
        num = _coerce_number(data.get(key))
        if num is not None:
            clean[key] = num

    iters = _coerce_number(data.get("max_design_iterations"))
    if iters is not None:
        clean["max_design_iterations"] = int(iters)

    if isinstance(data.get("stop_on_pass"), bool):
        clean["stop_on_pass"] = data["stop_on_pass"]
    if isinstance(data.get("notes"), str):
        clean["notes"] = data["notes"]

    return clean


# Back-compat alias — same as interpret_spec (LLM-only, no fallback).
interpret_spec_auto = interpret_spec


__all__ = [
    "DEFAULT_MODEL",
    "llm_unavailable_message",
    "interpret_spec",
    "interpret_spec_llm",
    "interpret_spec_auto",
]
