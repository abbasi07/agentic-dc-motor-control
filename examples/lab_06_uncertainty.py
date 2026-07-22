"""Lab 06 — uncertainty suite, recovery metrics, worst-case batch (OpenAI for NL→spec).

Run:
  uv run python examples/lab_06_uncertainty.py
"""

from __future__ import annotations

import argparse
import json

from _util import ensure_project_on_path, require_openai_or_exit

ensure_project_on_path()
require_openai_or_exit()

from agents import interpret_spec, tune_pid  # noqa: E402
from dc_motor import (  # noqa: E402
    FAILURE_TAGS,
    TAG_TO_ACTION_HINTS,
    evaluate_uncertainty_batch,
    failure_digest_from_scorecard,
    uncertainty_scenarios,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maxiter", type=int, default=8)
    args = parser.parse_args(argv)

    print("Tags:", FAILURE_TAGS)
    print("Hints:", json.dumps(TAG_TO_ACTION_HINTS, indent=2))
    print("Uncertainty scenarios:", [s.name for s in uncertainty_scenarios()])

    spec = interpret_spec(
        "settling <= 2.5s, overshoot < 12%, ss_error < 0.05, recovery_time_s <= 1.0, "
        "scenarios: step_1rads, load_disturbance, mismatch_load, noise_med"
    )
    print(spec.to_json())

    result = tune_pid(spec, method="auto", maxiter=args.maxiter)
    print("gains", result.gains.to_dict(), "pass", result.failure_digest.all_pass)
    print("tags", result.failure_digest.tags)
    print("hints", result.failure_digest.action_hints)

    for item in result.scorecard["scenarios"]:
        m = item["metrics"]
        settle = m["settling_time_s"]
        recovery = m["recovery_time_s"]
        settle_s = None if settle != settle else round(settle, 3)
        rec_s = None if recovery != recovery else round(recovery, 3)
        print(item["name"], "settle", settle_s, "recovery", rec_s, "pass", item["constraints"]["all_pass"])

    batch = evaluate_uncertainty_batch(
        result.controller,
        constraints=spec.constraints_for_evaluator(),
        score_weights=spec.score_weights_for_evaluator(),
    )
    print("Worst-case summary:", batch["summary"])
    digest = failure_digest_from_scorecard(batch)
    print("Batch tags:", digest.tags)
    print(digest.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
