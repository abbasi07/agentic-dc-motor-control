"""Lab 02 — shared evaluation harness / scorecards.

Run:
  uv run python examples/lab_02_evaluation.py
  uv run python examples/lab_02_evaluation.py --plot
"""

from __future__ import annotations

import argparse
from pprint import pprint

from _util import add_plot_arg, ensure_project_on_path, maybe_show

ensure_project_on_path()

from dc_motor import (  # noqa: E402
    PIDController,
    default_scenarios,
    evaluate_controller,
    scorecard_to_json,
)


def pick_better(a: dict, b: dict) -> dict:
    """Constraints-first, then lower mean scalar score."""
    ap = a["summary"]["all_constraints_pass"]
    bp = b["summary"]["all_constraints_pass"]
    if ap != bp:
        return a if ap else b
    return a if a["summary"]["mean_scalar_score"] <= b["summary"]["mean_scalar_score"] else b


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_plot_arg(parser)
    args = parser.parse_args(argv)
    maybe_show(args.plot)

    pid = PIDController(Kp=100.0, Ki=200.0, Kd=10.0, name="PID_CTMS_baseline")
    pid_soft = PIDController(Kp=40.0, Ki=80.0, Kd=5.0, name="PID_softer")
    scenarios = default_scenarios()

    print("Scenarios:")
    for sc in scenarios:
        print(f"  - {sc.name}: {sc.description}")

    card_a = evaluate_controller(pid, scenarios=scenarios)
    card_b = evaluate_controller(pid_soft, scenarios=scenarios)

    print("\nBaseline summary:")
    pprint(card_a["summary"])
    print(
        f"\n{card_a['controller']}: mean_score={card_a['summary']['mean_scalar_score']:.4f}, "
        f"all_pass={card_a['summary']['all_constraints_pass']}"
    )
    print(
        f"{card_b['controller']}: mean_score={card_b['summary']['mean_scalar_score']:.4f}, "
        f"all_pass={card_b['summary']['all_constraints_pass']}"
    )
    winner = pick_better(card_a, card_b)
    print("Selected:", winner["controller"])

    print("\nScorecard JSON (truncated):")
    print(scorecard_to_json(card_a)[:800], "...")

    if args.plot:
        import matplotlib.pyplot as plt

        step = next(s for s in card_a["scenarios"] if s["name"] == "step_1rads")
        tr = step["trajectories"]
        fig, axes = plt.subplots(3, 1, figsize=(8, 8), sharex=True)
        axes[0].plot(tr["t"], tr["omega"], lw=2)
        axes[0].plot(tr["t"], tr["reference"], ls="--")
        axes[0].set_ylabel("omega")
        axes[1].plot(tr["t"], tr["u"], color="C2", lw=2)
        axes[1].set_ylabel("u")
        axes[2].plot(tr["t"], tr["e"], color="C3", lw=2)
        axes[2].set_xlabel("t [s]")
        for a in axes:
            a.grid(True, alpha=0.3)
        fig.suptitle("Evaluation harness — step_1rads")
        plt.show()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
