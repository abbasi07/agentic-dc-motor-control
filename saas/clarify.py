"""Spec critique: clarifying questions from DesignSpec warnings (LLM + deterministic)."""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv

from agents.spec_agent import DEFAULT_MODEL, _require_api_key, llm_unavailable_message
from dc_motor.specs import DesignSpec

load_dotenv()


def deterministic_questions(spec: DesignSpec) -> list[str]:
    """Rule-based clarifying questions (no LLM). Always safe to call."""
    qs: list[str] = []
    for w in spec.warnings:
        qs.append(f"Spec warning: {w} — do you want to relax this requirement or drop a scenario?")

    settle = spec.hard_constraints.get("settling_time_s")
    if settle and "load_disturbance" in spec.required_scenarios:
        _op, lim = settle
        if lim < 2.0:
            qs.append(
                "Load disturbance starts at t=1.5 s, but settling limit is "
                f"{lim} s (absolute). Prefer settling >= 2.5 s on load tests, "
                "or remove settling as a hard constraint for load_disturbance?"
            )

    if not spec.hard_constraints:
        qs.append(
            "No hard constraints were extracted. Please state settling time, "
            "overshoot, and steady-state error limits."
        )

    if not spec.required_scenarios:
        qs.append("Which scenarios should be required (e.g. step_1rads, load_disturbance)?")

    if abs(spec.V_max) < 1e-9 and abs(spec.V_min) < 1e-9:
        qs.append("Voltage limits look unset. Confirm armature voltage bounds (default ±12 V).")

    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def critique_design_spec(
    spec: DesignSpec,
    *,
    plant_id: str = "dc_motor_ctms",
    use_llm: bool = True,
    model: str | None = None,
) -> dict[str, Any]:
    """Return clarifying questions. LLM may paraphrase; never invent metrics."""
    base_qs = deterministic_questions(spec)
    needs = bool(base_qs) or bool(spec.warnings)

    result: dict[str, Any] = {
        "needs_clarification": needs,
        "questions": list(base_qs),
        "warnings": list(spec.warnings),
        "source": "deterministic",
    }
    if not use_llm or not needs:
        return result

    try:
        key = _require_api_key()
    except RuntimeError as exc:
        result["llm_error"] = str(exc)
        return result

    from openai import OpenAI

    client = OpenAI(api_key=key)
    model_name = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    payload = {
        "plant_id": plant_id,
        "design_spec": spec.to_dict(),
        "seed_questions": base_qs,
    }
    system = (
        "You are a control-design Spec Critic for simulation-only controller design. "
        "Given a DesignSpec JSON and seed questions, return JSON: "
        '{"questions": ["..."], "summary": "one sentence"}. '
        "Only ask about missing/vague/infeasible requirements. "
        "Do NOT invent numeric metrics, pass/fail, or plant physics. "
        "Prefer at most 4 short questions."
    )
    try:
        response = client.chat.completions.create(
            model=model_name,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload)},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        result["llm_error"] = llm_unavailable_message(detail=str(exc))
        return result

    content = response.choices[0].message.content or "{}"
    data = json.loads(content)
    llm_qs = [str(q) for q in data.get("questions", []) if str(q).strip()]
    merged = list(base_qs)
    for q in llm_qs:
        if q not in merged:
            merged.append(q)
    result["questions"] = merged[:6]
    result["summary"] = str(data.get("summary", ""))
    result["source"] = "deterministic+llm"
    result["needs_clarification"] = bool(result["questions"])
    return result


__all__ = ["critique_design_spec", "deterministic_questions"]
