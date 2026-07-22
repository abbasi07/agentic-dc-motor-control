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
)

load_dotenv()

DEFAULT_MODEL = "gpt-5.4-nano"

SPEC_JSON_SCHEMA_HINT = {
    "raw_spec": "echo of user text",
    "hard_constraints": {
        "settling_time_s": {"op": "<=", "limit": 1.2},
        "overshoot_pct": {"op": "<=", "limit": 8.0},
        "steady_state_error": {"op": "<=", "limit": 0.05},
    },
    "soft_preferences": {
        "ITAE": 1.0,
        "control_effort": 0.05,
        "overshoot_pct": 0.05,
    },
    "required_scenarios": ["step_1rads", "load_disturbance"],
    "omega_ref": 1.0,
    "V_min": -12.0,
    "V_max": 12.0,
    "t_final": 3.0,
    "max_design_iterations": 5,
    "stop_on_pass": True,
    "notes": "short clarification of assumptions",
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


def _system_prompt() -> str:
    return f"""You are a control-systems Spec Interpreter for DC motor SPEED control simulation.
Convert the user's natural-language requirements into ONE JSON object matching this shape:
{json.dumps(SPEC_JSON_SCHEMA_HINT, indent=2)}

Rules:
- Only use metrics from: {sorted(ALLOWED_METRICS)}
- Only use scenarios from: {sorted(ALLOWED_SCENARIOS)}
- Operators allowed: <=, >=, <, >
- Prefer hard_constraints for must-meet limits; soft_preferences are lower-is-better weights for ranking.
- If the user mentions load/disturbance, include load_disturbance.
- If they mention uncertainty/mismatch/parameter variation, include plant_mismatch.
- If they mention noise/sensor, include noisy_measurement.
- Always include step_1rads unless the user explicitly excludes nominal step tests.
- Voltage limits default to ±12 V if unspecified.
- omega_ref default 1.0 rad/s if unspecified.
- Do NOT invent plant physics. Output JSON only (no markdown fences).
"""


def interpret_spec(
    text: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> DesignSpec:
    """OpenAI Spec Interpreter -> validated DesignSpec (no regex fallback)."""
    return interpret_spec_llm(text, model=model, api_key=api_key)


def interpret_spec_llm(
    text: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> DesignSpec:
    """OpenAI Spec Interpreter -> validated DesignSpec."""
    key = _require_api_key(api_key)

    from openai import OpenAI

    client = OpenAI(api_key=key)
    model_name = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

    try:
        response = client.chat.completions.create(
            model=model_name,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": text},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        msg = llm_unavailable_message(detail=str(exc))
        print(msg)
        raise RuntimeError(msg) from exc

    content = response.choices[0].message.content or "{}"
    data: dict[str, Any] = json.loads(content)
    data["raw_spec"] = text
    return design_spec_from_dict(data, raw_spec=text, source="llm")


# Back-compat alias — same as interpret_spec (LLM-only, no fallback).
interpret_spec_auto = interpret_spec


__all__ = [
    "DEFAULT_MODEL",
    "llm_unavailable_message",
    "interpret_spec",
    "interpret_spec_llm",
    "interpret_spec_auto",
]
