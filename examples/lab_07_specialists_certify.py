"""Lab 07 — specialists + simulation certification / export (OpenAI for NL→spec).

Run:
  uv run python examples/lab_07_specialists_certify.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _util import ensure_project_on_path, project_root, require_openai_or_exit

ensure_project_on_path()
require_openai_or_exit()

from agents import (  # noqa: E402
    certify_candidate,
    design_adaptive,
    design_mpc,
    design_robust_pid,
    export_certified_package,
    identify_plant_sim,
    interpret_spec,
    run_design_session,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=project_root() / "exports",
        help="Directory for certification export packages (gitignored).",
    )
    parser.add_argument("--max-iterations", type=int, default=6)
    args = parser.parse_args(argv)

    spec = interpret_spec(
        "settling <= 2.5s, overshoot < 12%, ss_error < 0.05, "
        "scenarios: step_1rads, load_disturbance, plant_mismatch"
    )

    robust = design_robust_pid(spec)
    mpc = design_mpc(spec)
    adap = design_adaptive(spec)
    print("Plant ID:", identify_plant_sim())

    for name, cand in [("robust", robust), ("mpc", mpc), ("adaptive", adap)]:
        print(
            name,
            "kind",
            cand.kind,
            "pass",
            cand.failure_digest.all_pass,
            "obj",
            round(cand.objective, 4),
            "tags",
            cand.failure_digest.tags,
        )

    sess = run_design_session(
        "settling <= 2.5s, overshoot < 12%, ss_error < 0.05, "
        "scenarios: step_1rads, plant_mismatch",
        mode="heuristic",
        max_iterations=args.max_iterations,
    )
    print("status", sess.status, "actions", [a.action for a in sess.action_trace])
    print("best kind", None if sess.best is None else sess.best.kind)

    if sess.best is None:
        print("No candidate to certify.")
        return 1

    cert = certify_candidate(sess.best)
    print(cert.reason)

    out = export_certified_package(
        sess.best,
        rationale=sess.rationale,
        out_dir=args.export_dir,
        nl_spec=sess.nl_spec,
        action_trace=[a.to_dict() for a in sess.action_trace],
    )
    print("export path:", out)
    return 0 if cert.allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
