"""Adaptive design orchestrator: Spec -> action -> tool -> diagnose -> redesign.

Pillars P2+P3 (+ P4 certification handoff). Tools compute metrics; the LLM or
heuristic policy only chooses the next *action* and writes a grounded rationale.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from dotenv import load_dotenv

from dc_motor.evaluate import evaluate_controller, scorecard_to_json
from dc_motor.failure import FailureDigest, TAG_TO_ACTION_HINTS
from dc_motor.plant import CTMS_PARAMS, MotorParams
from dc_motor.scenarios import scenarios_from_spec
from dc_motor.specs import DesignSpec, suggest_t_final, validate_and_clamp_design_spec

from .controller_registry import SPECIALIST_ACTIONS, registry_metadata
from .critic import diagnose
from .design_candidate import DesignCandidate, candidate_from_controller, candidate_from_tune_result
from .pid_tuner import grid_search_pid, optimize_pid, tune_pid
from .specialists import (
    design_adaptive,
    design_fuzzy,
    design_lqg,
    design_lqr,
    design_mpc,
    design_mrac,
    design_robust_pid,
    run_identify_plant,
)
from .spec_agent import interpret_spec, llm_unavailable_message

load_dotenv()

OrchestratorMode = Literal["script", "heuristic", "llm"]

ORCH_LAB_GRID: dict[str, list[float]] = {
    "Kp": [60.0, 100.0, 160.0, 220.0],
    "Ki": [100.0, 200.0, 350.0, 500.0],
    "Kd": [0.0, 10.0, 25.0],
}

AVAILABLE_ACTIONS = (
    "tune_pid_grid",
    "tune_pid_scipy",
    "tune_pid_auto",
    "expand_scenarios",
    "relax_settling_for_load",
    "call_robust",
    "call_lqr",
    "call_lqg",
    "call_mpc",
    "call_mrac",
    "call_fuzzy",
    "call_rl",  # legacy alias for the adaptive family (-> MRAC)
    "identify_plant",
    "stop",
)


@dataclass
class ActionPlan:
    action: str
    reason: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "reason": self.reason, "params": dict(self.params)}


@dataclass
class ActionRecord:
    iteration: int
    action: str
    reason: str
    wall_time_s: float
    all_pass: bool | None
    objective: float | None
    n_evaluations: int
    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    digest_summary: str = ""
    digest_tags: list[str] = field(default_factory=list)
    gains: dict[str, float] | None = None
    kind: str | None = None
    notes: str = ""
    policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DesignSession:
    nl_spec: str
    mode: str
    spec: DesignSpec
    status: str
    action_trace: list[ActionRecord] = field(default_factory=list)
    best: DesignCandidate | None = None
    rationale: str = ""
    total_wall_time_s: float = 0.0
    total_tool_evaluations: int = 0
    total_tokens: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, *, include_scorecard_json: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "nl_spec": self.nl_spec,
            "mode": self.mode,
            "status": self.status,
            "spec": self.spec.to_dict(),
            "action_trace": [a.to_dict() for a in self.action_trace],
            "rationale": self.rationale,
            "total_wall_time_s": self.total_wall_time_s,
            "total_tool_evaluations": self.total_tool_evaluations,
            "total_tokens": self.total_tokens,
            "warnings": list(self.warnings),
            "best": None if self.best is None else self.best.to_dict(include_scorecard_json=False),
        }
        if include_scorecard_json and self.best is not None:
            out["best_scorecard_json"] = scorecard_to_json(self.best.scorecard)
        return out

    def to_json(self, indent: int = 2, *, include_scorecard_json: bool = False) -> str:
        return json.dumps(self.to_dict(include_scorecard_json=include_scorecard_json), indent=indent)


@dataclass
class _SessionState:
    spec: DesignSpec
    best: DesignCandidate | None = None
    actions_tried: list[str] = field(default_factory=list)
    last_digest: FailureDigest | None = None
    iteration: int = 0


def _better(a: DesignCandidate | None, b: DesignCandidate) -> DesignCandidate:
    if a is None:
        return b
    return b if b.objective < a.objective else a


def _params_for_record(best: DesignCandidate | None) -> dict[str, float] | None:
    if best is None:
        return None
    g = best.gains
    if g is not None:
        return g.to_dict()
    out = {k: float(v) for k, v in best.params.items() if isinstance(v, (int, float))}
    return out or None


def _revaluate_best(
    state: _SessionState,
    *,
    base_params: MotorParams,
    note: str,
    plant_factory=None,
) -> int:
    if state.best is None:
        return 0
    ctrl = state.best.controller
    scorecard = evaluate_controller(
        ctrl,
        scenarios=scenarios_from_spec(state.spec),
        constraints=state.spec.constraints_for_evaluator(),
        score_weights=state.spec.score_weights_for_evaluator(),
        base_params=base_params,
        plant_factory=plant_factory,
    )
    rebuilt = candidate_from_controller(
        ctrl,
        scorecard,
        kind=state.best.kind,
        params=dict(state.best.params),
        method=state.best.method + "+reval",
        n_evaluations=1,
        notes=note,
    )
    state.best = rebuilt
    state.last_digest = rebuilt.failure_digest
    return 1


def expand_scenarios(spec: DesignSpec, *, add: list[str] | None = None) -> DesignSpec:
    extra = add or ["plant_mismatch", "noisy_measurement", "mismatch_load", "noise_med"]
    scenarios = list(spec.required_scenarios)
    for name in extra:
        if name not in scenarios:
            scenarios.append(name)
    if "step_1rads" not in scenarios:
        scenarios.insert(0, "step_1rads")
    from dataclasses import replace

    updated = replace(
        spec,
        required_scenarios=scenarios,
        notes=(spec.notes + " | expand_scenarios").strip(" |"),
    )
    return validate_and_clamp_design_spec(updated)


def relax_settling_for_load(spec: DesignSpec, *, new_limit: float = 2.5) -> DesignSpec:
    hard = dict(spec.hard_constraints)
    op = "<="
    if "settling_time_s" in hard:
        op = hard["settling_time_s"][0]
        if op not in {"<=", "<"}:
            op = "<="
    hard["settling_time_s"] = (op, float(new_limit))
    # Derive the horizon from the (new) settling target instead of a fixed floor so
    # a large settling time (slow motors) gets a proportionally long horizon.
    new_t_final = suggest_t_final(settling=float(new_limit), current=spec.t_final)
    from dataclasses import replace

    provenance = dict(spec.provenance)
    provenance["settling_time_s"] = "user"  # an explicit relax is a user decision
    provenance["t_final"] = "derived"
    updated = replace(
        spec,
        hard_constraints=hard,
        t_final=new_t_final,
        notes=(spec.notes + f" | relax_settling_for_load->{new_limit}").strip(" |"),
        warnings=list(spec.warnings)
        + [f"Relaxed settling_time_s to {op} {new_limit} for load feasibility."],
        provenance=provenance,
    )
    return validate_and_clamp_design_spec(updated)


# Orchestrator action -> specialist designer. call_rl is a legacy alias for the
# adaptive family, which is now a proper MRAC (design_mrac == design_adaptive).
_SPECIALIST_DESIGNERS = {
    "call_robust": design_robust_pid,
    "call_lqr": design_lqr,
    "call_lqg": design_lqg,
    "call_mpc": design_mpc,
    "call_mrac": design_mrac,
    "call_fuzzy": design_fuzzy,
    "call_rl": design_adaptive,
}


def _execute_action(
    plan: ActionPlan,
    state: _SessionState,
    *,
    grid: dict[str, list[float]],
    maxiter: int,
    seed: int,
    base_params: MotorParams,
    plant_factory=None,
) -> tuple[_SessionState, ActionRecord]:
    t0 = time.perf_counter()
    action = plan.action
    notes = ""
    n_eval = 0
    all_pass: bool | None = None
    objective: float | None = None

    if action not in AVAILABLE_ACTIONS:
        notes = f"Unknown action {action!r}; treated as stop."
        action = "stop"

    if action == "stop":
        notes = notes or plan.reason
    elif action == "expand_scenarios":
        state.spec = expand_scenarios(state.spec, add=plan.params.get("add"))
        notes = f"Scenarios now: {state.spec.required_scenarios}"
        n_eval = _revaluate_best(
            state,
            base_params=base_params,
            note="Re-evaluated after expand_scenarios",
            plant_factory=plant_factory,
        )
    elif action == "relax_settling_for_load":
        new_limit = float(plan.params.get("new_limit", 2.5))
        state.spec = relax_settling_for_load(state.spec, new_limit=new_limit)
        notes = f"Spec settling relaxed; warnings={state.spec.warnings[-1:]}"
        n_eval = _revaluate_best(
            state,
            base_params=base_params,
            note="Re-evaluated after relax_settling_for_load",
            plant_factory=plant_factory,
        )
    elif action == "tune_pid_grid":
        result = grid_search_pid(
            state.spec,
            grid=grid,
            base_params=base_params,
            stop_on_pass=True,
            plant_factory=plant_factory,
        )
        cand = candidate_from_tune_result(result)
        n_eval = result.n_evaluations
        state.best = _better(state.best, cand)
        state.last_digest = state.best.failure_digest
        notes = result.notes
    elif action == "tune_pid_scipy":
        warm = state.best.gains if state.best is not None else None
        result = optimize_pid(
            state.spec,
            method="differential_evolution",
            maxiter=int(plan.params.get("maxiter", maxiter)),
            seed=seed,
            base_params=base_params,
            warm_start=warm,
            plant_factory=plant_factory,
        )
        cand = candidate_from_tune_result(result)
        n_eval = result.n_evaluations
        state.best = _better(state.best, cand)
        state.last_digest = state.best.failure_digest
        notes = result.notes
    elif action == "tune_pid_auto":
        result = tune_pid(
            state.spec,
            method="auto",
            grid=grid,
            maxiter=int(plan.params.get("maxiter", maxiter)),
            seed=seed,
            base_params=base_params,
            plant_factory=plant_factory,
        )
        cand = candidate_from_tune_result(result)
        n_eval = result.n_evaluations
        state.best = _better(state.best, cand)
        state.last_digest = state.best.failure_digest
        notes = result.notes
    elif action in _SPECIALIST_DESIGNERS:
        designer = _SPECIALIST_DESIGNERS[action]
        if action == "call_robust":
            cand = designer(
                state.spec, base_params=base_params, seed=seed, plant_factory=plant_factory
            )
        else:
            cand = designer(state.spec, base_params=base_params, plant_factory=plant_factory)
        n_eval = cand.n_evaluations
        state.best = _better(state.best, cand)
        state.last_digest = state.best.failure_digest
        notes = cand.notes
    elif action == "identify_plant":
        cand = run_identify_plant(state.spec, base_params=base_params, plant_factory=plant_factory)
        n_eval = cand.n_evaluations
        state.best = _better(state.best, cand)
        state.last_digest = state.best.failure_digest
        notes = cand.notes
    else:
        notes = f"Unhandled action {action}"

    digest = state.last_digest
    if state.best is not None and action != "stop":
        all_pass = state.best.failure_digest.all_pass
        objective = state.best.objective

    state.actions_tried.append(action)
    record = ActionRecord(
        iteration=state.iteration,
        action=action,
        reason=plan.reason,
        wall_time_s=time.perf_counter() - t0,
        all_pass=all_pass,
        objective=objective,
        n_evaluations=n_eval,
        digest_summary="" if digest is None else digest.summary,
        digest_tags=[] if digest is None else list(digest.tags),
        gains=_params_for_record(state.best),
        kind=None if state.best is None else state.best.kind,
        notes=notes,
    )
    return state, record


def heuristic_choose_action(state: _SessionState) -> ActionPlan:
    """Hand-coded diagnose→redesign policy (ablation B; no LLM).

    Uses the grounded critic to translate the current FailureDigest into an ordered
    list of candidate actions (tag hints first, then controller families whose
    strengths match the failure pattern), skipping anything already tried.
    """
    digest = state.last_digest
    tried = set(state.actions_tried)

    if state.best is not None and state.best.failure_digest.all_pass:
        return ActionPlan("stop", "Hard constraints passed; stop.")

    if digest is not None and "POSSIBLY_INFEASIBLE_SPEC" in digest.tags:
        if "relax_settling_for_load" not in tried:
            return ActionPlan(
                "relax_settling_for_load",
                "Settling limit conflicts with load onset; relax settling.",
                {"new_limit": 2.5},
            )

    if not tried:
        return ActionPlan("tune_pid_auto", "First action: constraint-aware PID auto-tune.")

    if state.actions_tried and state.actions_tried[-1] == "relax_settling_for_load":
        if digest is None or not digest.all_pass:
            return ActionPlan(
                "tune_pid_auto",
                "Spec relaxed; re-run auto-tune under updated constraints.",
            )

    # Grounded diagnosis: ordered recommended actions (hints + matching families).
    if state.best is not None:
        diag = diagnose(state.best, state.spec, tried_actions=tuple(state.actions_tried))
        for action in diag.recommended_actions:
            if action not in tried and action in AVAILABLE_ACTIONS and action != "stop":
                return ActionPlan(
                    action,
                    f"Critic: tags={diag.tags} → try {action}.",
                    {"maxiter": 12} if action == "tune_pid_scipy" else {},
                )

    # Last-resort refine if a SciPy pass was never attempted.
    if "tune_pid_scipy" not in tried and digest is not None and not digest.all_pass:
        return ActionPlan("tune_pid_scipy", "Fallback: SciPy refine before stopping.", {"maxiter": 12})

    return ActionPlan("stop", "Heuristic policy exhausted available tools.")


def _controller_menu() -> str:
    lines = []
    for fam in registry_metadata():
        lines.append(
            f"- {fam['action']}: {fam['label']} — {fam['description']} "
            f"(good for {fam['addresses_tags']})"
        )
    return "\n".join(lines)


def _orchestrator_system_prompt() -> str:
    return f"""You are the adaptive control-design orchestrator for a DC motor SPEED simulation.
Choose the NEXT tool action only. Never invent gains, metrics, or pass/fail — the
tools compute every number and you read them from the state you are given.

Available actions: {list(AVAILABLE_ACTIONS)}

Controller families (each is a real design tool behind reset()/step()):
{_controller_menu()}

Other actions: tune_pid_grid/scipy/auto (PID search), expand_scenarios,
relax_settling_for_load, identify_plant (sim ID), stop.

Policy: diagnose the failure pattern, then either tweak parameters (tune_pid_*),
switch controller structure to a family whose strengths match the failing tags, or
relax a physically infeasible spec. Prefer the grounded diagnosis 'recommended_actions'
unless you have a better justification.

Tag→action hints: {json.dumps(TAG_TO_ACTION_HINTS)}

Return ONE JSON object:
{{"action": "<name>", "reason": "<short>", "params": {{}}}}
"""


def llm_choose_action(
    state: _SessionState,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[ActionPlan, int, int]:
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key or key.startswith("sk-your-key"):
        msg = llm_unavailable_message(detail="OPENAI_API_KEY missing for orchestrator.")
        print(msg)
        raise RuntimeError(msg)

    from openai import OpenAI

    client = OpenAI(api_key=key)
    model_name = model or os.getenv("OPENAI_MODEL", "gpt-5.4-nano")
    payload = {
        "spec": state.spec.to_dict(),
        "iteration": state.iteration,
        "actions_tried": state.actions_tried,
        "best_kind": None if state.best is None else state.best.kind,
        "best_params": None if state.best is None else state.best.params,
        "best_all_pass": None if state.best is None else state.best.failure_digest.all_pass,
        "best_objective": None if state.best is None else state.best.objective,
        "failure_digest": None if state.last_digest is None else state.last_digest.to_dict(),
        "diagnosis": (
            None
            if state.best is None
            else diagnose(
                state.best, state.spec, tried_actions=tuple(state.actions_tried)
            ).to_dict()
        ),
        "available_actions": list(AVAILABLE_ACTIONS),
    }
    response = client.chat.completions.create(
        model=model_name,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _orchestrator_system_prompt()},
            {
                "role": "user",
                "content": "Choose the next action given this state:\n"
                + json.dumps(payload, indent=2),
            },
        ],
    )
    usage = response.usage
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    data = json.loads(response.choices[0].message.content or "{}")
    action = str(data.get("action", "stop"))
    if action not in AVAILABLE_ACTIONS:
        action = "stop"
    params = data.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return (
        ActionPlan(action=action, reason=str(data.get("reason", "LLM decision")), params=params),
        prompt_tokens,
        completion_tokens,
    )


def grounded_rationale(best: DesignCandidate, spec: DesignSpec, *, mode: str) -> str:
    digest = best.failure_digest
    lines = [
        f"Design rationale ({mode})",
        f"- Spec source: {spec.source}",
        f"- Controller: {getattr(best.controller, 'name', best.kind)} ({best.kind})",
        f"- Params: {best.params}",
        f"- Method: {best.method}",
        f"- all_constraints_pass: {digest.all_pass}",
        f"- mean_scalar_score: {digest.mean_scalar_score:.6g}",
        f"- scenarios: {spec.required_scenarios}",
    ]
    for item in best.scorecard.get("scenarios", []):
        checks = item["constraints"]["checks"]
        parts = [
            f"{metric}={check['value']:.4g} ({check['op']} {check['limit']}, pass={check['pass']})"
            for metric, check in checks.items()
        ]
        lines.append(f"- {item['name']}: " + "; ".join(parts))
    if digest.tags:
        lines.append(f"- tags: {digest.tags}")
    lines.append(
        "- Claim basis: numbers from evaluate_controller / FailureDigest only. "
        "Simulation certification only — no hardware."
    )
    return "\n".join(lines)


def llm_write_rationale(
    best: DesignCandidate,
    spec: DesignSpec,
    action_trace: list[ActionRecord],
    *,
    model: str | None = None,
) -> tuple[str, int]:
    """LLM rationale citing scorecard evidence only. Raises if OpenAI unavailable."""
    key = os.getenv("OPENAI_API_KEY")
    if not key or key.startswith("sk-your-key"):
        msg = llm_unavailable_message(detail="OPENAI_API_KEY missing for rationale.")
        print(msg)
        raise RuntimeError(msg)

    from openai import OpenAI

    client = OpenAI(api_key=key)
    model_name = model or os.getenv("OPENAI_MODEL", "gpt-5.4-nano")
    evidence = {
        "spec": spec.to_dict(),
        "kind": best.kind,
        "params": best.params,
        "failure_digest": best.failure_digest.to_dict(),
        "scorecard_summary": best.scorecard.get("summary"),
        "per_scenario_checks": [
            {
                "name": item["name"],
                "all_pass": item["constraints"]["all_pass"],
                "checks": item["constraints"]["checks"],
                "scalar_score": item["scalar_score"],
            }
            for item in best.scorecard.get("scenarios", [])
        ],
        "action_trace": [a.to_dict() for a in action_trace],
    }
    try:
        response = client.chat.completions.create(
            model=model_name,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write a short design rationale for a simulated DC motor speed controller. "
                        "Cite ONLY numbers in the evidence JSON. Do not invent metrics. "
                        "Do not claim hardware readiness."
                    ),
                },
                {"role": "user", "content": json.dumps(evidence, indent=2)},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        msg = llm_unavailable_message(detail=str(exc))
        print(msg)
        raise RuntimeError(msg) from exc

    usage = response.usage
    tokens = int(getattr(usage, "total_tokens", 0) or 0)
    text = response.choices[0].message.content or grounded_rationale(best, spec, mode="empty_llm")
    return text, tokens


def run_design_session(
    nl_spec: str,
    *,
    mode: OrchestratorMode = "heuristic",
    max_iterations: int | None = None,
    grid: dict[str, list[float]] | None = None,
    maxiter_scipy: int = 10,
    seed: int = 0,
    base_params: MotorParams = CTMS_PARAMS,
    model: str | None = None,
    spec: DesignSpec | None = None,
    plant_id: str | None = None,
) -> DesignSession:
    """Run Spec -> adaptive redesign loop.

    Spec interpretation always uses OpenAI unless ``spec`` is provided (SaaS confirm path).
    Modes differ only in *action policy*:
      script     — one-shot tune_pid_auto (ablation A)
      heuristic  — hand-coded if/else redesign (ablation B)
      llm        — OpenAI chooses actions (ablation C)
    """
    t_session = time.perf_counter()
    warnings: list[str] = []

    if plant_id:
        from dc_motor.registry import get_plant_factory, motor_params_for

        base_params = motor_params_for(plant_id)
        plant_factory = get_plant_factory(plant_id)
    else:
        plant_factory = None

    # NL -> DesignSpec via OpenAI, or use a caller-confirmed DesignSpec
    if spec is None:
        spec = interpret_spec(nl_spec, model=model)
    else:
        spec = validate_and_clamp_design_spec(spec)
    effective_mode: str = mode

    budget = int(max_iterations if max_iterations is not None else spec.max_design_iterations)
    budget = max(1, min(budget, 20))
    use_grid = grid or ORCH_LAB_GRID
    state = _SessionState(spec=spec)
    trace: list[ActionRecord] = []
    total_tokens = 0
    total_evals = 0
    status = "budget_exhausted"

    exec_kwargs = dict(
        grid=use_grid,
        maxiter=maxiter_scipy,
        seed=seed,
        base_params=base_params,
        plant_factory=plant_factory,
    )

    if effective_mode == "script":
        plan = ActionPlan("tune_pid_auto", "Fixed-script baseline: one-shot auto tune.")
        state.iteration = 1
        state, record = _execute_action(plan, state, **exec_kwargs)
        record.policy = "script"
        trace.append(record)
        total_evals += record.n_evaluations
        status = (
            "passed"
            if state.best is not None and state.best.failure_digest.all_pass
            else "budget_exhausted"
        )
    else:
        for i in range(1, budget + 1):
            state.iteration = i
            token_p = token_c = 0
            if effective_mode == "llm":
                plan, token_p, token_c = llm_choose_action(state, model=model)
                policy = "llm"
            else:
                plan = heuristic_choose_action(state)
                policy = "heuristic"

            total_tokens += token_p + token_c
            state, record = _execute_action(plan, state, **exec_kwargs)
            record.policy = policy
            record.tokens_prompt = token_p or None
            record.tokens_completion = token_c or None
            trace.append(record)
            total_evals += record.n_evaluations

            if plan.action == "stop":
                status = (
                    "passed"
                    if state.best is not None and state.best.failure_digest.all_pass
                    else "stopped"
                )
                break

            if (
                state.best is not None
                and state.best.failure_digest.all_pass
                and state.spec.stop_on_pass
            ):
                status = "passed"
                if effective_mode == "heuristic":
                    stop_plan = heuristic_choose_action(state)
                    if stop_plan.action == "stop":
                        trace.append(
                            ActionRecord(
                                iteration=i,
                                action="stop",
                                reason=stop_plan.reason,
                                wall_time_s=0.0,
                                all_pass=True,
                                objective=state.best.objective,
                                n_evaluations=0,
                                digest_summary=state.best.failure_digest.summary,
                                digest_tags=list(state.best.failure_digest.tags),
                                gains=_params_for_record(state.best),
                                kind=state.best.kind,
                                notes="Auto-stop after pass (heuristic).",
                                policy=policy,
                            )
                        )
                    break
                continue
        else:
            status = (
                "passed"
                if state.best is not None and state.best.failure_digest.all_pass
                else "budget_exhausted"
            )

    rationale = ""
    if state.best is not None:
        if effective_mode == "llm" and status == "passed":
            rationale, tok = llm_write_rationale(state.best, state.spec, trace, model=model)
            total_tokens += tok
        else:
            # Deterministic citation of scorecard numbers (not an LLM fallback)
            rationale = grounded_rationale(state.best, state.spec, mode=effective_mode)

    return DesignSession(
        nl_spec=nl_spec,
        mode=effective_mode,
        spec=state.spec,
        status=status,
        action_trace=trace,
        best=state.best,
        rationale=rationale,
        total_wall_time_s=time.perf_counter() - t_session,
        total_tool_evaluations=total_evals,
        total_tokens=total_tokens,
        warnings=warnings + list(state.spec.warnings),
    )


__all__ = [
    "AVAILABLE_ACTIONS",
    "ORCH_LAB_GRID",
    "ActionPlan",
    "ActionRecord",
    "DesignSession",
    "expand_scenarios",
    "relax_settling_for_load",
    "heuristic_choose_action",
    "llm_choose_action",
    "grounded_rationale",
    "run_design_session",
]
