"""Lab 08 — ablation study (script vs heuristic vs llm). OpenAI required for NL→spec.

Run (compact subset):
  uv run python examples/lab_08_ablation.py
  uv run python examples/lab_08_ablation.py --full
  uv run python examples/lab_08_ablation.py --modes script heuristic
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _util import ensure_project_on_path, project_root, require_openai_or_exit

ensure_project_on_path()
require_openai_or_exit()

from experiments import (  # noqa: E402
    BENCHMARK_PROMPTS,
    ablation_comparison_table,
    run_ablation,
    save_ablation_report,
    summarize_ablation,
)


COMPACT_IDS = {"easy_step", "load_feasible", "load_infeasible_settle", "mismatch"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Run all BENCHMARK_PROMPTS.")
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["script", "heuristic", "llm"],
        choices=["script", "heuristic", "llm"],
    )
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--maxiter-scipy", type=int, default=6)
    parser.add_argument(
        "--out",
        type=Path,
        default=project_root() / "exports" / "ablation_lab08.json",
    )
    args = parser.parse_args(argv)

    print("Prompts:", [p["id"] for p in BENCHMARK_PROMPTS])
    prompts = (
        BENCHMARK_PROMPTS
        if args.full
        else [p for p in BENCHMARK_PROMPTS if p["id"] in COMPACT_IDS]
    )

    rows = run_ablation(
        modes=args.modes,
        prompts=prompts,
        max_iterations=args.max_iterations,
        maxiter_scipy=args.maxiter_scipy,
    )
    summary = summarize_ablation(rows)
    print(json.dumps(summary, indent=2))
    print("\nComparison table:")
    for row in ablation_comparison_table(rows):
        print(row)
    for r in rows:
        print(
            f"{r.prompt_id:24s} {r.mode:12s} pass={r.passed} "
            f"actions={r.actions} kind={r.kind}"
        )

    path = save_ablation_report(rows, args.out)
    print("saved", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
