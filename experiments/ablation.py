"""NL-spec benchmark suite and ablation runner (Phase 9 / pillar P5)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from agents.orchestrator import run_design_session

Mode = Literal["script", "heuristic", "llm"]

# Held-out NL prompts for reproducible ablation
BENCHMARK_PROMPTS: list[dict[str, str]] = [
    {
        "id": "easy_step",
        "text": "Settle under 1.2 s, overshoot < 8%, ss_error < 0.05, scenarios: step_1rads.",
    },
    {
        "id": "load_feasible",
        "text": (
            "settling <= 2.5s, overshoot < 10%, ss_error < 0.05, "
            "scenarios: step_1rads, load_disturbance."
        ),
    },
    {
        "id": "load_infeasible_settle",
        "text": (
            "Settle under 1.2 s, overshoot < 8%, ss_error < 0.05, "
            "scenarios: step_1rads, load_disturbance."
        ),
    },
    {
        "id": "mismatch",
        "text": (
            "settling <= 2.0s, overshoot < 15%, ss_error < 0.05, "
            "scenarios: step_1rads, plant_mismatch."
        ),
    },
    {
        "id": "noise",
        "text": (
            "settling <= 2.0s, overshoot < 12%, ss_error < 0.08, "
            "scenarios: step_1rads, noisy_measurement."
        ),
    },
    {
        "id": "combined_stress",
        "text": (
            "settling <= 2.5s, overshoot < 12%, ss_error < 0.05, "
            "scenarios: step_1rads, mismatch_load, noise_med."
        ),
    },
    {
        "id": "aggressive_step",
        "text": (
            "Aggressive step tracking: settling < 0.9s, overshoot < 5%, "
            "ss_error < 0.03, scenarios: step_1rads. Prefer low effort."
        ),
    },
    {
        "id": "soft_load",
        "text": (
            "settling <= 3.0s, overshoot < 15%, ss_error < 0.08, "
            "scenarios: step_1rads, load_disturbance. Prefer low control effort."
        ),
    },
    {
        "id": "harsh_mismatch",
        "text": (
            "settling <= 2.5s, overshoot < 12%, ss_error < 0.05, "
            "scenarios: step_1rads, mismatch_harsh."
        ),
    },
    {
        "id": "noise_high",
        "text": (
            "settling <= 2.5s, overshoot < 15%, ss_error < 0.1, "
            "scenarios: step_1rads, noise_high."
        ),
    },
]


@dataclass
class AblationRow:
    prompt_id: str
    mode: str
    status: str
    passed: bool
    n_actions: int
    n_evals: int
    tokens: int
    wall_s: float
    actions: list[str] = field(default_factory=list)
    kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_ablation(
    *,
    modes: list[Mode] | None = None,
    prompts: list[dict[str, str]] | None = None,
    max_iterations: int = 5,
    maxiter_scipy: int = 6,
    plant_id: str | None = None,
) -> list[AblationRow]:
    """Compare pipelines A/B/C on the same NL-spec suite."""
    modes = modes or ["script", "heuristic", "llm"]
    prompts = prompts or BENCHMARK_PROMPTS
    rows: list[AblationRow] = []
    for p in prompts:
        for mode in modes:
            t0 = time.perf_counter()
            sess = run_design_session(
                p["text"],
                mode=mode,
                max_iterations=max_iterations,
                maxiter_scipy=maxiter_scipy,
                plant_id=plant_id,
            )
            rows.append(
                AblationRow(
                    prompt_id=p["id"],
                    mode=sess.mode,
                    status=sess.status,
                    passed=bool(sess.best and sess.best.failure_digest.all_pass),
                    n_actions=len(sess.action_trace),
                    n_evals=sess.total_tool_evaluations,
                    tokens=sess.total_tokens,
                    wall_s=round(time.perf_counter() - t0, 3),
                    actions=[a.action for a in sess.action_trace],
                    kind=None if sess.best is None else sess.best.kind,
                )
            )
    return rows


def summarize_ablation(rows: list[AblationRow]) -> dict[str, Any]:
    by_mode: dict[str, list[AblationRow]] = {}
    for r in rows:
        by_mode.setdefault(r.mode, []).append(r)
    summary: dict[str, Any] = {}
    for mode, group in by_mode.items():
        n = len(group)
        summary[mode] = {
            "n_prompts": n,
            "success_rate": sum(1 for r in group if r.passed) / n if n else 0.0,
            "mean_actions": sum(r.n_actions for r in group) / n if n else 0.0,
            "mean_evals": sum(r.n_evals for r in group) / n if n else 0.0,
            "mean_wall_s": sum(r.wall_s for r in group) / n if n else 0.0,
            "total_tokens": sum(r.tokens for r in group),
        }
    # Pairwise deltas vs script baseline when present
    if "script" in summary:
        base = summary["script"]["success_rate"]
        for mode, stats in summary.items():
            if mode == "script":
                continue
            stats["success_rate_delta_vs_script"] = stats["success_rate"] - base
    return summary


def ablation_comparison_table(rows: list[AblationRow]) -> list[dict[str, Any]]:
    """One row per prompt with columns for each mode (pass / actions / wall_s)."""
    by_prompt: dict[str, dict[str, AblationRow]] = {}
    for r in rows:
        by_prompt.setdefault(r.prompt_id, {})[r.mode] = r
    table: list[dict[str, Any]] = []
    for prompt_id, modes in sorted(by_prompt.items()):
        row: dict[str, Any] = {"prompt_id": prompt_id}
        for mode, r in modes.items():
            row[f"{mode}_pass"] = r.passed
            row[f"{mode}_actions"] = r.n_actions
            row[f"{mode}_wall_s"] = r.wall_s
            row[f"{mode}_kind"] = r.kind
        table.append(row)
    return table


def save_ablation_report(
    rows: list[AblationRow],
    out_path: str | Path,
) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "rows": [r.to_dict() for r in rows],
        "summary": summarize_ablation(rows),
        "comparison_table": ablation_comparison_table(rows),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


__all__ = [
    "BENCHMARK_PROMPTS",
    "AblationRow",
    "ablation_comparison_table",
    "run_ablation",
    "save_ablation_report",
    "summarize_ablation",
]
