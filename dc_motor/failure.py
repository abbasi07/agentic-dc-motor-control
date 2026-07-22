"""Structured FailureDigest from scorecards — deterministic diagnosis for redesign."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

# Documented taxonomy for orchestrator redesign (Phase 6)
FAILURE_TAGS = (
    "TRACKING_SLOW",
    "OVERSHOOT",
    "DISTURBANCE_REJECT_FAIL",
    "FRAGILE_TO_MISMATCH",
    "NOISE_SENSITIVE",
    "SATURATION_HEAVY",
    "MODEL_DISTRUST",
    "POSSIBLY_INFEASIBLE_SPEC",
    "RECOVERY_SLOW",
)

# Tag -> suggested redesign action (documented policy for LLM + heuristic)
TAG_TO_ACTION_HINTS: dict[str, list[str]] = {
    "TRACKING_SLOW": ["tune_pid_scipy", "tune_pid_auto"],
    "OVERSHOOT": ["tune_pid_grid", "call_robust"],
    "DISTURBANCE_REJECT_FAIL": ["tune_pid_scipy", "call_mpc", "call_rl"],
    "FRAGILE_TO_MISMATCH": ["call_robust", "expand_scenarios"],
    "NOISE_SENSITIVE": ["call_robust", "tune_pid_grid"],
    "SATURATION_HEAVY": ["call_mpc", "call_robust"],
    "MODEL_DISTRUST": ["identify_plant", "call_robust", "expand_scenarios"],
    "POSSIBLY_INFEASIBLE_SPEC": ["relax_settling_for_load"],
    "RECOVERY_SLOW": ["tune_pid_scipy", "call_mpc", "call_rl"],
}


@dataclass
class FailureItem:
    """One constraint check that failed (or passed with margin recorded)."""

    scenario: str
    metric: str
    value: float | None
    op: str
    limit: float
    margin: float
    """Signed distance to the limit. Positive => violated for <=/<; negative => slack."""
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FailureDigest:
    """Orchestrator-facing summary of which scenarios/metrics failed and by how much."""

    all_pass: bool
    failed_scenarios: list[str] = field(default_factory=list)
    failures: list[FailureItem] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    action_hints: list[str] = field(default_factory=list)
    mean_scalar_score: float = float("nan")
    n_failures: int = 0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_pass": self.all_pass,
            "failed_scenarios": list(self.failed_scenarios),
            "failures": [f.to_dict() for f in self.failures],
            "tags": list(self.tags),
            "action_hints": list(self.action_hints),
            "mean_scalar_score": self.mean_scalar_score,
            "n_failures": self.n_failures,
            "summary": self.summary,
        }


def _margin(value: float | None, op: str, limit: float) -> float:
    """Positive margin means the constraint is violated (or infeasible / NaN)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return float("inf")
    if op in {"<=", "<"}:
        return float(value) - float(limit)
    if op in {">=", ">"}:
        return float(limit) - float(value)
    return float("inf")


def _tag_failures(
    failures: list[FailureItem],
    failed_scenarios: list[str],
    scorecard: dict[str, Any] | None = None,
) -> list[str]:
    """Failure tagging taxonomy for redesign hints (deterministic)."""
    tags: list[str] = []
    metrics_failed = {f.metric for f in failures if not f.passed}

    if "settling_time_s" in metrics_failed or "rise_time_s" in metrics_failed:
        tags.append("TRACKING_SLOW")
    if "overshoot_pct" in metrics_failed:
        tags.append("OVERSHOOT")
    if "recovery_time_s" in metrics_failed:
        tags.append("RECOVERY_SLOW")
    if any(s in failed_scenarios for s in ("load_disturbance", "mismatch_load")):
        tags.append("DISTURBANCE_REJECT_FAIL")
    if any(s in failed_scenarios for s in ("plant_mismatch", "mismatch_harsh", "mismatch_load")):
        tags.append("FRAGILE_TO_MISMATCH")
    if any(s.startswith("noise") or s == "noisy_measurement" for s in failed_scenarios):
        tags.append("NOISE_SENSITIVE")
    if "saturation_time_s" in metrics_failed:
        tags.append("SATURATION_HEAVY")

    # Combined stress / harsh mismatch => model distrust
    if "mismatch_load" in failed_scenarios or "mismatch_harsh" in failed_scenarios:
        tags.append("MODEL_DISTRUST")
    elif (
        "plant_mismatch" in failed_scenarios
        and "load_disturbance" in failed_scenarios
    ):
        tags.append("MODEL_DISTRUST")

    for f in failures:
        if (
            not f.passed
            and f.scenario in {"load_disturbance", "mismatch_load"}
            and f.metric == "settling_time_s"
            and f.op in {"<=", "<"}
            and f.limit < 1.5
        ):
            tags.append("POSSIBLY_INFEASIBLE_SPEC")
            break

    # Soft saturation signal from metrics even if not constrained
    if scorecard is not None and "SATURATION_HEAVY" not in tags:
        for item in scorecard.get("scenarios", []):
            sat = item.get("metrics", {}).get("saturation_time_s", 0.0)
            if isinstance(sat, (int, float)) and not math.isnan(sat) and sat > 0.8:
                tags.append("SATURATION_HEAVY")
                break

    seen: set[str] = set()
    ordered: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def action_hints_from_tags(tags: list[str]) -> list[str]:
    """Map tags -> unique suggested actions (documented policy)."""
    hints: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        for action in TAG_TO_ACTION_HINTS.get(tag, []):
            if action not in seen:
                seen.add(action)
                hints.append(action)
    return hints


def failure_digest_from_scorecard(scorecard: dict[str, Any]) -> FailureDigest:
    """Build FailureDigest from an evaluate_controller scorecard (no trajectories needed)."""
    failures: list[FailureItem] = []
    failed_scenarios: list[str] = []

    for item in scorecard.get("scenarios", []):
        name = item.get("name", "?")
        constraint_block = item.get("constraints", {})
        checks = constraint_block.get("checks", {})
        scenario_failed = not bool(constraint_block.get("all_pass", False))
        if scenario_failed:
            failed_scenarios.append(name)

        for metric, check in checks.items():
            val = check.get("value")
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                val = None
            elif val is not None:
                val = float(val)
            op = str(check.get("op", "<="))
            limit = float(check.get("limit"))
            passed = bool(check.get("pass", False))
            margin = _margin(val, op, limit)
            if not passed:
                failures.append(
                    FailureItem(
                        scenario=name,
                        metric=metric,
                        value=val,
                        op=op,
                        limit=limit,
                        margin=margin,
                        passed=False,
                    )
                )

    summary_block = scorecard.get("summary", {})
    all_pass = bool(summary_block.get("all_constraints_pass", len(failures) == 0))
    mean_score = float(summary_block.get("mean_scalar_score", float("nan")))
    tags = _tag_failures(failures, failed_scenarios, scorecard)
    hints = action_hints_from_tags(tags)

    if all_pass:
        summary = (
            f"All hard constraints passed across {summary_block.get('n_scenarios', '?')} "
            f"scenario(s); mean_scalar_score={mean_score:.4f}; "
            f"pass_rate={summary_block.get('pass_rate', 1.0)}; "
            f"worst_ITAE={summary_block.get('worst_case_ITAE', float('nan'))}."
        )
    else:
        parts = [
            f"{len(failures)} constraint failure(s) in scenarios {failed_scenarios}; "
            f"tags={tags}; hints={hints}."
        ]
        ranked = sorted(
            failures,
            key=lambda f: f.margin if math.isfinite(f.margin) else 1e9,
            reverse=True,
        )
        for f in ranked[:3]:
            val_s = "nan" if f.value is None else f"{f.value:.4g}"
            parts.append(
                f"{f.scenario}.{f.metric}={val_s} (need {f.op} {f.limit}, margin={f.margin:.4g})"
            )
        summary = " ".join(parts)

    return FailureDigest(
        all_pass=all_pass,
        failed_scenarios=failed_scenarios,
        failures=failures,
        tags=tags,
        action_hints=hints,
        mean_scalar_score=mean_score,
        n_failures=len(failures),
        summary=summary,
    )
