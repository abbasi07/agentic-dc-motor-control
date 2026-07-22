"""Lab 04 — constraint-aware PID tuner + FailureDigest (OpenAI for NL→spec).

Run:
  uv run python examples/lab_04_pid_tuner.py
  uv run python examples/lab_04_pid_tuner.py --plot
"""

from __future__ import annotations

import argparse
import json

from _util import add_plot_arg, ensure_project_on_path, maybe_show, require_openai_or_exit

ensure_project_on_path()
require_openai_or_exit()

from agents import PIDGains, evaluate_pid_gains, grid_search_pid, interpret_spec, tune_pid  # noqa: E402
from dc_motor import failure_digest_from_scorecard  # noqa: E402


LAB_GRID = {
    "Kp": [60.0, 100.0, 160.0, 220.0],
    "Ki": [100.0, 200.0, 350.0, 500.0],
    "Kd": [0.0, 10.0, 25.0],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_plot_arg(parser)
    args = parser.parse_args(argv)
    maybe_show(args.plot)

    nl = (
        "Settle under 1.2 s, overshoot < 8%, ss_error < 0.05, "
        "scenarios: step_1rads. Prefer low effort."
    )
    spec = interpret_spec(nl)
    print("SOURCE:", spec.source)
    print(spec.to_json())

    baseline = evaluate_pid_gains(PIDGains(Kp=100.0, Ki=200.0, Kd=10.0), spec)
    digest = failure_digest_from_scorecard(baseline)
    print("\nBaseline FailureDigest:")
    print(json.dumps(digest.to_dict(), indent=2))

    grid_result = grid_search_pid(spec, grid=LAB_GRID, stop_on_pass=True)
    print("\nGrid search:", grid_result.gains.to_dict(), "pass=", grid_result.failure_digest.all_pass)

    auto_result = tune_pid(spec, method="auto", grid=LAB_GRID, maxiter=8, seed=0)
    print("Auto tune:", auto_result.gains.to_dict(), "pass=", auto_result.failure_digest.all_pass)

    nl_load = (
        "settling <= 2.5s, overshoot < 10%, ss_error < 0.05, "
        "scenarios: step_1rads, load_disturbance. Prefer low effort."
    )
    spec_load = interpret_spec(nl_load)
    load_result = tune_pid(spec_load, method="auto", grid=LAB_GRID, maxiter=8, seed=0)
    print("\nLoad-aware tune:", load_result.gains.to_dict())
    print("tags:", load_result.failure_digest.tags)
    print("summary:", load_result.failure_digest.summary)

    nl_bad = (
        "Settle under 1.2 s, overshoot < 8%, ss_error < 0.05, "
        "scenarios: step_1rads, load_disturbance."
    )
    spec_bad = interpret_spec(nl_bad)
    bad_result = grid_search_pid(spec_bad, grid=LAB_GRID, stop_on_pass=False)
    print("\nTight settling + load (often infeasible):")
    print("tags:", bad_result.failure_digest.tags)
    print("digest:", bad_result.failure_digest.summary)

    if args.plot:
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
        for item in load_result.scorecard["scenarios"]:
            tr = item["trajectories"]
            axes[0].plot(tr["t"], tr["omega"], label=item["name"])
            axes[1].plot(tr["t"], tr["u"], label=item["name"])
        axes[0].axhline(spec_load.omega_ref, color="k", ls="--", lw=0.8)
        axes[0].set_ylabel("omega")
        axes[0].legend()
        axes[0].set_title(f"Tuned PID {load_result.gains.to_dict()}")
        axes[1].set_ylabel("u [V]")
        axes[1].set_xlabel("t [s]")
        axes[1].legend()
        plt.tight_layout()
        plt.show()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
