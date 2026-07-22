"""Plant Interpreter Agent: natural-language motor description -> MotorModel.

Mirrors ``spec_agent``: OpenAI-only, no regex fallback. The user can describe a DC
motor in plain language ("R=2 ohm, L=0.5 H, J=0.02, b=0.1, Kt=Ke=0.023, 24 V bus")
or by naming a known device, and this returns a *validated* ``MotorModel`` ready for
simulation and feasibility analysis. If the LLM is unavailable a clear message is
printed and RuntimeError is raised.

The LLM only extracts / normalizes numbers into SI units — it never invents physics
that the user did not provide, and every result is re-validated by
``motor_model.build_motor_model`` (which enforces positivity and sane ranges).
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv

from agents.spec_agent import DEFAULT_MODEL, _require_api_key, llm_unavailable_message
from dc_motor.motor_model import MotorModel, MotorModelValidationError, build_motor_model

load_dotenv()

PLANT_JSON_SCHEMA_HINT = {
    "name": "short human label for the motor",
    "J": 0.01,
    "b": 0.1,
    "K": 0.01,
    "R": 1.0,
    "L": 0.5,
    "V_max": 12.0,
    "notes": "assumptions / unit conversions you applied",
}

_PARAM_KEYS = ("J", "b", "K", "R", "L")


def _system_prompt() -> str:
    return f"""You are a Plant Interpreter for armature-controlled DC motor SPEED control.
Convert the user's natural-language motor description into ONE JSON object shaped like:
{json.dumps(PLANT_JSON_SCHEMA_HINT, indent=2)}

Physical meaning (all SI units):
- J: rotor moment of inertia [kg·m^2]
- b: viscous friction coefficient [N·m·s/rad]
- K: torque constant Kt = back-emf constant Ke [N·m/A = V·s/rad]
- R: armature resistance [ohm]
- L: armature inductance [H]
- V_max: available armature voltage magnitude [V] (bus / driver limit)

Rules:
- Convert any stated units to SI (e.g. mH -> H, g·cm^2 -> kg·m^2, oz·in -> N·m).
- If the user gives Kt and Ke separately and they differ slightly, use their average for K.
- If a parameter is genuinely not provided and cannot be inferred, use a reasonable
  small-motor default and mention it in notes. Never output zero or negative values.
- V_max default is 12 V if unspecified.
- Output JSON only (no markdown fences, no prose).
"""


def interpret_plant(
    text: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> MotorModel:
    """OpenAI Plant Interpreter -> validated MotorModel (no regex fallback)."""
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
    return motor_model_from_dict(data, source="llm")


def motor_model_from_dict(data: dict[str, Any], *, source: str = "manual") -> MotorModel:
    """Build + validate a MotorModel from a JSON-ish dict (LLM or API payload)."""
    missing = [k for k in _PARAM_KEYS if data.get(k) is None]
    if missing:
        raise MotorModelValidationError(
            f"Missing required motor parameter(s): {', '.join(missing)}"
        )
    return build_motor_model(
        J=float(data["J"]),
        b=float(data["b"]),
        K=float(data["K"]),
        R=float(data["R"]),
        L=float(data["L"]),
        V_max=float(data.get("V_max", 12.0)),
        name=str(data.get("name", "custom_dc_motor")) or "custom_dc_motor",
        source=source,
        notes=str(data.get("notes", "")),
    )


__all__ = [
    "PLANT_JSON_SCHEMA_HINT",
    "interpret_plant",
    "motor_model_from_dict",
]
