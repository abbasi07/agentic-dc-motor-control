"""Lab 03 — Spec Interpreter: NL → DesignSpec (OpenAI required).

Run:
  uv run python examples/lab_03_spec_agent.py
"""

from __future__ import annotations

import argparse

from _util import ensure_project_on_path, require_openai_or_exit

ensure_project_on_path()
require_openai_or_exit()

from agents import interpret_spec  # noqa: E402
from dc_motor import PIDController, evaluate_controller, scenarios_by_names  # noqa: E402


EXAMPLES = [
    (
        "Settle under 1.2 s, overshoot < 8%, ss_error < 0.05, voltage <= 12 V, "
        "scenarios: step_1rads, load_disturbance. Prefer low effort."
    ),
    "Track 1 rad/s with settling <= 2s and overshoot below 15%. Include mismatch and noise.",
    "Aggressive: settling < 0.8s, overshoot < 5%. Test load disturbance.",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    for text in EXAMPLES:
        spec = interpret_spec(text)
        print("=" * 70)
        print("SOURCE:", spec.source)
        print(spec.to_json())
        if spec.warnings:
            print("warnings:", spec.warnings)

    text = EXAMPLES[0]
    spec = interpret_spec(text)
    selected = scenarios_by_names(spec.required_scenarios)
    pid = PIDController(
        Kp=100.0,
        Ki=200.0,
        Kd=10.0,
        V_min=spec.V_min,
        V_max=spec.V_max,
        name="PID_CTMS_baseline",
    )
    scorecard = evaluate_controller(
        pid,
        scenarios=selected,
        constraints=spec.constraints_for_evaluator(),
        score_weights=spec.score_weights_for_evaluator(),
    )
    print("\nWired DesignSpec → evaluate_controller:")
    print("scenarios:", spec.required_scenarios)
    print("summary:", scorecard["summary"])
    for item in scorecard["scenarios"]:
        print(
            f"  {item['name']}: pass={item['constraints']['all_pass']} "
            f"score={item['scalar_score']:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
