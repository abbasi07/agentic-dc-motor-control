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
    CONTROLLER_FAMILIES,
    certify_candidate,
    design_by_type,
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

    print("Plant ID:", identify_plant_sim())

    # Design every registered controller family via the pluggable registry.
    print("\nController families (pluggable registry):")
    for fam in CONTROLLER_FAMILIES:
        cand = design_by_type(fam.type_name, spec)
        print(
            f"  {fam.type_name:8s} kind={cand.kind:11s} "
            f"pass={cand.failure_digest.all_pass!s:5s} "
            f"obj={round(cand.objective, 4)} tags={cand.failure_digest.tags}"
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
