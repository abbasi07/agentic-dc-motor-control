"""Seed a DesignSpec onto a job WITHOUT calling OpenAI (E2.6 plumbing e2e helper).

``interpret_spec`` is OpenAI-only by design, so to exercise the async run/worker/SSE/
export plumbing offline we inject a validated :class:`DesignSpec` straight through the
active job store (the DB-backed repository when ``COPILOT_PERSIST`` is on).

Usage (inside the api container so it shares Postgres):
    uv run python scripts/seed_spec.py <job_id> [omega_ref]
"""

from __future__ import annotations

import sys

from dc_motor.specs import design_spec_from_dict
from saas.jobs import get_job_store


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: seed_spec.py <job_id> [omega_ref]")
    job_id = sys.argv[1]
    omega_ref = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    store = get_job_store()
    job = store.get(job_id)

    spec = design_spec_from_dict(
        {
            "raw_spec": (
                f"Reach about {omega_ref:g} rad/s, settle under 2 s, "
                "overshoot under 20%, steady-state error under 5%."
            ),
            "hard_constraints": {
                "settling_time_s": {"op": "<=", "limit": 2.0},
                "overshoot_pct": {"op": "<=", "limit": 20.0},
                "steady_state_error": {"op": "<=", "limit": 0.05},
            },
            "omega_ref": omega_ref,
        },
        source="manual",
    )
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.nl_spec = spec.raw_spec
    job.status = "spec_ready"
    job.spec_confirmed = True
    job.touch()
    store.save(job)
    print(f"seeded job={job_id} status={job.status} rev={job._rev}")
    print("hard_constraints:", job.spec_dict["hard_constraints"])


if __name__ == "__main__":
    main()
