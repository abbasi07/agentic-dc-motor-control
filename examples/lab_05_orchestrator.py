"""Lab 05 — adaptive orchestrator (script / heuristic / llm). OpenAI required for NL→spec.

Run:
  uv run python examples/lab_05_orchestrator.py
  uv run python examples/lab_05_orchestrator.py --modes script heuristic
  uv run python examples/lab_05_orchestrator.py --modes llm --max-iterations 6
"""

from __future__ import annotations

import argparse
import json

from _util import ensure_project_on_path, require_openai_or_exit

ensure_project_on_path()
require_openai_or_exit()

from agents import AVAILABLE_ACTIONS, run_design_session  # noqa: E402


NL_EASY = (
    "Settle under 1.2 s, overshoot < 8%, ss_error < 0.05, "
    "scenarios: step_1rads. Prefer low effort."
)
NL_HARD = (
    "Settle under 1.2 s, overshoot < 8%, ss_error < 0.05, "
    "scenarios: step_1rads, load_disturbance."
)


def _print_session(name: str, sess) -> None:
    print("=" * 70)
    print(
        name,
        "status=",
        sess.status,
        "evals=",
        sess.total_tool_evaluations,
        "wall_s=",
        round(sess.total_wall_time_s, 2),
        "tokens=",
        sess.total_tokens,
    )
    print("actions:", [(a.action, a.all_pass) for a in sess.action_trace])
    if sess.best is not None:
        print("kind:", sess.best.kind, "pass:", sess.best.failure_digest.all_pass)
        if sess.best.gains is not None:
            print("gains:", sess.best.gains.to_dict())
    if sess.rationale:
        print("rationale:\n", sess.rationale[:800])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["script", "heuristic"],
        choices=["script", "heuristic", "llm"],
        help="Orchestrator action policies to compare (default: script heuristic).",
    )
    parser.add_argument("--max-iterations", type=int, default=6)
    parser.add_argument("--maxiter-scipy", type=int, default=8)
    parser.add_argument(
        "--prompt",
        choices=["easy", "hard"],
        default="hard",
        help="easy = step only; hard = step + load (shows redesign).",
    )
    args = parser.parse_args(argv)

    print("Action menu:", AVAILABLE_ACTIONS)
    nl = NL_EASY if args.prompt == "easy" else NL_HARD
    print("NL:", nl)

    rows = []
    for mode in args.modes:
        sess = run_design_session(
            nl,
            mode=mode,
            max_iterations=args.max_iterations,
            maxiter_scipy=args.maxiter_scipy,
        )
        _print_session(mode, sess)
        rows.append(
            {
                "mode": sess.mode,
                "status": sess.status,
                "passed": bool(sess.best and sess.best.failure_digest.all_pass),
                "n_actions": len(sess.action_trace),
                "n_evals": sess.total_tool_evaluations,
                "tokens": sess.total_tokens,
                "wall_s": round(sess.total_wall_time_s, 2),
                "actions": [a.action for a in sess.action_trace],
            }
        )

    print("\nComparison:")
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
