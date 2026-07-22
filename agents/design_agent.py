"""Chat-first tool-calling Design Agent (workstream D).

A single OpenAI function-calling loop that *owns a design session* and exposes the
existing deterministic engine as typed tools:

    define_plant       — describe ANY DC motor in chat (NL) or by numbers -> validated plant
    set_spec           — natural-language performance goals -> validated DesignSpec
    check_feasibility  — physics-based feasibility of the spec on the current motor
    design_controller  — design a controller of a chosen type (pid/robust/mpc/adaptive/auto)
    simulate           — (re)run the deterministic simulation of the current controller
    query_results      — answer questions STRICTLY from the stored scorecard (never invents)
    modify             — apply a requested change to the spec (relax settling, add tests, …)
    export             — code-enforced certification gate + export package (no hardware)

Hard rules (mirrors the rest of the project):
- Tools COMPUTE every number. The LLM plans, phrases, and chooses which tool to call.
- ``query_results`` is fully deterministic: it only returns numbers that already live
  in the stored scorecard / session — it can never fabricate a metric.
- NL -> spec / NL -> motor stay OpenAI-only (no regex fallback). The *tools*, however,
  are plain Python and can be driven deterministically without OpenAI (used by tests
  and as the non-LLM fallback control path).
- Simulation / certification only — never claims hardware readiness.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from dc_motor.evaluate import evaluate_controller
from dc_motor.feasibility import check_feasibility
from dc_motor.scenarios import scenarios_from_spec
from dc_motor.specs import DesignSpec, design_spec_from_dict

from .certify import certify_candidate
from .design_candidate import DesignCandidate, candidate_from_controller, candidate_from_tune_result
from .orchestrator import DesignSession, grounded_rationale
from .pid_tuner import tune_pid
from .spec_agent import DEFAULT_MODEL, interpret_spec, llm_unavailable_message
from .specialists import design_adaptive, design_mpc, design_robust_pid

load_dotenv()

# Controller families a user can explicitly pick (design_controller(type=...)).
CONTROLLER_TYPES = ("auto", "pid", "robust", "mpc", "adaptive")

# Digit-free scenario phrases so grounded answers never emit a stray number that is
# not actually a measured value (e.g. the "1" inside "step_1rads").
_SCENARIO_PHRASE = {
    "step_1rads": "the nominal step response",
    "load_disturbance": "the load-disturbance test",
    "plant_mismatch": "the plant-mismatch test",
    "noisy_measurement": "the noisy-measurement test",
    "mismatch_load": "the mismatch-plus-load test",
    "noise_low": "the low-noise test",
    "noise_med": "the medium-noise test",
    "noise_high": "the high-noise test",
    "mismatch_harsh": "the harsh-mismatch test",
}

# Digit-free, human-readable metric labels + units (kept local so grounded answer
# strings contain no numeric tokens other than the measured values themselves).
_METRIC_LABEL = {
    "rise_time_s": "rise time",
    "settling_time_s": "settling time",
    "overshoot_pct": "overshoot",
    "steady_state_error": "steady-state error",
    "IAE": "IAE",
    "ISE": "ISE",
    "ITAE": "ITAE",
    "control_effort": "control effort",
    "saturation_time_s": "time in voltage saturation",
    "recovery_time_s": "recovery time after the disturbance",
}
_METRIC_UNIT = {
    "rise_time_s": "s",
    "settling_time_s": "s",
    "overshoot_pct": "%",
    "steady_state_error": "",
    "IAE": "",
    "ISE": "",
    "ITAE": "",
    "control_effort": "",
    "saturation_time_s": "s",
    "recovery_time_s": "s",
}

# Question keyword -> canonical metric (longest keys checked first).
_QUERY_METRIC_ALIASES = {
    "settling time": "settling_time_s",
    "settling": "settling_time_s",
    "settle": "settling_time_s",
    "rise time": "rise_time_s",
    "rise": "rise_time_s",
    "overshoot": "overshoot_pct",
    "steady-state error": "steady_state_error",
    "steady state error": "steady_state_error",
    "steady state": "steady_state_error",
    "ss error": "steady_state_error",
    "tracking error": "steady_state_error",
    "itae": "ITAE",
    "iae": "IAE",
    "ise": "ISE",
    "control effort": "control_effort",
    "effort": "control_effort",
    "saturation": "saturation_time_s",
    "saturated": "saturation_time_s",
    "recovery time": "recovery_time_s",
    "recovery": "recovery_time_s",
}

# Question keyword -> scenario name.
_QUERY_SCENARIO_ALIASES = {
    "nominal step": "step_1rads",
    "step response": "step_1rads",
    "step": "step_1rads",
    "nominal": "step_1rads",
    "load disturbance": "load_disturbance",
    "disturbance": "load_disturbance",
    "load": "load_disturbance",
    "harsh mismatch": "mismatch_harsh",
    "mismatch plus load": "mismatch_load",
    "mismatch + load": "mismatch_load",
    "mismatch": "plant_mismatch",
    "uncertainty": "plant_mismatch",
    "uncertain": "plant_mismatch",
    "noisy": "noisy_measurement",
    "noise": "noisy_measurement",
    "sensor": "noisy_measurement",
}

_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _fmt(value: Any) -> str:
    """Format a scorecard number for display without inventing precision."""
    if value is None:
        return "n/a"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(v):
        return "n/a"
    if v == int(v) and abs(v) < 1e6:
        return str(int(v))
    return f"{v:.4g}"


# --------------------------------------------------------------------------- #
# Grounding helpers (deterministic — used by query_results and its tests)
# --------------------------------------------------------------------------- #
def scorecard_numbers(scorecard: dict[str, Any] | None) -> set[float]:
    """Every finite number that appears anywhere in a scorecard.

    This is the *ground truth* set: ``query_results`` may never emit a number that
    is not close to one of these values.
    """
    nums: set[float] = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, bool):
            return
        if isinstance(obj, (int, float)):
            v = float(obj)
            if math.isfinite(v):
                nums.add(round(v, 10))
            return
        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v)

    _walk(scorecard or {})
    return nums


def _is_grounded(value: float, allowed: set[float], *, rel_tol: float = 1e-3, abs_tol: float = 1e-3) -> bool:
    return any(math.isclose(value, a, rel_tol=rel_tol, abs_tol=abs_tol) for a in allowed)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
@dataclass
class DesignAgentSession:
    """Owns one chat-first design session: a DesignJob + the OpenAI transcript.

    The job (from ``saas.jobs``) holds all persisted artifacts (motor, spec,
    feasibility, scorecard, certification, export path) so answers survive across
    turns. ``messages`` is the OpenAI function-calling transcript; ``tool_log``
    records every tool call for auditing / ablation.
    """

    job: Any  # saas.jobs.DesignJob (imported lazily to avoid a hard cycle)
    model: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    total_tokens: int = 0

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def create(
        cls,
        *,
        plant_id: str = "dc_motor_ctms",
        mode: str = "heuristic",
        model: str | None = None,
    ) -> "DesignAgentSession":
        from saas.jobs import DesignJob

        job = DesignJob(job_id=str(uuid.uuid4()), plant_id=plant_id, mode=mode)
        return cls(job=job, model=model)

    # ------------------------------------------------------------------ #
    # Engine targeting (custom chat-defined motor vs registry plant)
    # ------------------------------------------------------------------ #
    def _engine_targets(self):
        from saas.service import effective_motor_params

        use_custom = self.job._motor is not None or self.job.motor_dict is not None
        base_params = effective_motor_params(self.job)
        if use_custom:
            return base_params, None
        from dc_motor.registry import get_plant_factory

        return base_params, get_plant_factory(self.job.plant_id)

    def _require_spec(self) -> DesignSpec:
        job = self.job
        if job._spec is None and job.spec_dict is not None:
            job._spec = design_spec_from_dict(job.spec_dict, raw_spec=job.nl_spec, source="manual")
        if job._spec is None:
            raise RuntimeError("No performance spec yet. Call set_spec first.")
        return job._spec

    # ------------------------------------------------------------------ #
    # Deterministic helper: inject a spec without the LLM (tests / fallback)
    # ------------------------------------------------------------------ #
    def load_spec(self, spec: DesignSpec) -> None:
        self.job._spec = spec
        self.job.spec_dict = spec.to_dict()
        if not self.job.nl_spec:
            self.job.nl_spec = spec.raw_spec

    # ================================================================== #
    # TOOLS  (each returns a JSON-serializable dict)
    # ================================================================== #
    def define_plant(
        self,
        description: str | None = None,
        *,
        J: float | None = None,
        b: float | None = None,
        K: float | None = None,
        R: float | None = None,
        L: float | None = None,
        V_max: float | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Define the DC motor to control — either from NL text or explicit numbers."""
        from saas.service import set_motor_from_params, set_motor_from_text

        numeric = {"J": J, "b": b, "K": K, "R": R, "L": L}
        has_numbers = all(v is not None for v in numeric.values())

        if has_numbers:
            payload = {k: float(v) for k, v in numeric.items()}
            payload["V_max"] = float(V_max) if V_max is not None else 12.0
            payload["name"] = name or "custom_dc_motor"
            set_motor_from_params(self.job, payload)
        elif description:
            # NL -> validated MotorModel (OpenAI-only, re-validated by physics).
            set_motor_from_text(self.job, description)
        else:
            return {"error": "Provide either a text description or all of J, b, K, R, L."}

        motor = self.job.motor_dict or {}
        chars = motor.get("characteristics", {})
        return {
            "motor": motor.get("name"),
            "params": motor.get("params"),
            "V_max": motor.get("V_max"),
            "characteristics": {
                "omega_max_rad_s": chars.get("omega_max_rad_s"),
                "dc_gain": chars.get("dc_gain"),
                "tau_mech_s": chars.get("tau_mech_s"),
                "tau_elec_s": chars.get("tau_elec_s"),
            },
            "warnings": motor.get("warnings", []),
        }

    def set_spec(self, text: str) -> dict[str, Any]:
        """Interpret natural-language performance goals into a validated DesignSpec."""
        spec = interpret_spec(text, model=self.model)
        job = self.job
        job.nl_spec = text.strip()
        job._spec = spec
        job.spec_dict = spec.to_dict()
        job.confirmed = False
        feas = self.check_feasibility()
        return {
            "spec": spec.to_dict(),
            "feasibility": feas,
            "notes": spec.notes,
            "warnings": list(spec.warnings),
        }

    def check_feasibility(self) -> dict[str, Any]:
        """Physics-based feasibility of the current spec on the current motor."""
        from saas.service import effective_motor_params

        spec = self._require_spec()
        params = effective_motor_params(self.job)
        report = check_feasibility(params, spec)
        self.job.feasibility = report.to_dict()
        return self.job.feasibility

    def design_controller(
        self,
        controller_type: str = "auto",
        *,
        mode: str = "heuristic",
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        """Design a controller of the requested type and score it deterministically."""
        controller_type = (controller_type or "auto").lower().strip()
        if controller_type not in CONTROLLER_TYPES:
            return {
                "error": f"Unknown controller_type {controller_type!r}. "
                f"Choose one of {list(CONTROLLER_TYPES)}."
            }

        spec = self._require_spec()
        job = self.job

        if controller_type == "auto":
            # Full adaptive orchestrator loop (may itself switch topology). Reuse the
            # SaaS job runner so custom-vs-registry routing + storage stay in one place.
            from saas.service import confirm_and_run

            job.mode = mode
            confirm_and_run(job, max_iterations=max_iterations)
            return self._design_summary(controller_type)

        # Explicit controller family requested by the engineer.
        job.confirmed = True
        job.status = "running"
        job.error = None
        base_params, plant_factory = self._engine_targets()
        t0 = time.perf_counter()
        cand = self._design_single(controller_type, spec, base_params, plant_factory)
        status = "passed" if cand.failure_digest.all_pass else "budget_exhausted"
        session = DesignSession(
            nl_spec=job.nl_spec or spec.raw_spec,
            mode=f"agent:{controller_type}",
            spec=spec,
            status=status,
            action_trace=[],
            best=cand,
            rationale=grounded_rationale(cand, spec, mode=f"agent:{controller_type}"),
            total_wall_time_s=time.perf_counter() - t0,
            total_tool_evaluations=cand.n_evaluations,
            total_tokens=0,
            warnings=list(spec.warnings),
        )
        self._store_session(session)
        return self._design_summary(controller_type)

    @staticmethod
    def _design_single(
        controller_type: str,
        spec: DesignSpec,
        base_params,
        plant_factory,
    ) -> DesignCandidate:
        if controller_type == "pid":
            result = tune_pid(
                spec, method="auto", base_params=base_params, plant_factory=plant_factory
            )
            return candidate_from_tune_result(result)
        if controller_type == "robust":
            return design_robust_pid(spec, base_params=base_params, plant_factory=plant_factory)
        if controller_type == "mpc":
            return design_mpc(spec, base_params=base_params, plant_factory=plant_factory)
        if controller_type == "adaptive":
            return design_adaptive(spec, base_params=base_params, plant_factory=plant_factory)
        raise ValueError(f"Unhandled controller_type {controller_type!r}")

    def _store_session(self, session: DesignSession) -> None:
        job = self.job
        job._session = session
        job._spec = session.spec
        job.spec_dict = session.spec.to_dict()
        job.session_dict = session.to_dict(include_scorecard_json=False)
        job.scorecard = None if session.best is None else session.best.scorecard
        job.certification = (
            None if session.best is None else certify_candidate(session.best).to_dict()
        )
        job.status = "completed"
        job.touch()

    def simulate(self) -> dict[str, Any]:
        """Re-run the deterministic simulation of the current controller under the spec."""
        job = self.job
        if job._session is None or job._session.best is None:
            return {"error": "No controller yet. Call design_controller first."}
        spec = self._require_spec()
        base_params, plant_factory = self._engine_targets()
        best = job._session.best
        scorecard = evaluate_controller(
            best.controller,
            scenarios=scenarios_from_spec(spec),
            constraints=spec.constraints_for_evaluator(),
            score_weights=spec.score_weights_for_evaluator(),
            base_params=base_params,
            plant_factory=plant_factory,
        )
        rebuilt = candidate_from_controller(
            best.controller,
            scorecard,
            kind=best.kind,
            params=dict(best.params),
            method=best.method + "+simulate",
            n_evaluations=1,
            notes="Re-simulated by query/simulate tool.",
        )
        job._session.best = rebuilt
        job.scorecard = scorecard
        job.certification = certify_candidate(rebuilt).to_dict()
        job.touch()
        return self._design_summary(rebuilt.kind)

    def query_results(self, question: str) -> dict[str, Any]:
        """Answer a question STRICTLY from the stored scorecard (never invents numbers)."""
        job = self.job
        sc = job.scorecard
        if not sc or not sc.get("scenarios"):
            return {
                "grounded": True,
                "answer": (
                    "There are no simulation results yet. Design a controller first, "
                    "then I can report exact numbers from the scorecard."
                ),
                "facts": [],
            }

        q = (question or "").lower()
        available = [item["name"] for item in sc["scenarios"]]
        summary = sc.get("summary", {})

        # 1) Which scenario(s) does the question reference?
        scenarios = [
            name
            for kw, name in _QUERY_SCENARIO_ALIASES.items()
            if kw in q and name in available
        ]
        # de-dup preserving order
        scenarios = list(dict.fromkeys(scenarios))
        wants_all = any(w in q for w in ("all", "every", "each", "worst"))

        # 2) Which metric(s)?
        metrics: list[str] = []
        for kw in sorted(_QUERY_METRIC_ALIASES, key=len, reverse=True):
            if kw in q:
                metric = _QUERY_METRIC_ALIASES[kw]
                if metric not in metrics:
                    metrics.append(metric)

        # 3) Pass/fail / certification questions
        wants_status = any(
            w in q for w in ("pass", "fail", "meet", "requirement", "certif", "allowed", "ok")
        )

        facts: list[dict[str, Any]] = []
        lines: list[str] = []

        if metrics:
            target_scenarios = scenarios or (["step_1rads"] if "step_1rads" in available else available[:1])
            if wants_all:
                target_scenarios = available
            for sc_name in target_scenarios:
                item = next((i for i in sc["scenarios"] if i["name"] == sc_name), None)
                if item is None:
                    lines.append(f"{_scenario_phrase(sc_name)} was not part of the test set.")
                    continue
                checks = item.get("constraints", {}).get("checks", {})
                for metric in metrics:
                    value = item.get("metrics", {}).get(metric)
                    unit = _METRIC_UNIT.get(metric, "")
                    label = _METRIC_LABEL.get(metric, metric)
                    check = checks.get(metric)
                    fact = {
                        "scenario": sc_name,
                        "metric": metric,
                        "value": None if value is None else float(value),
                        "unit": unit,
                        "source": "scorecard",
                    }
                    text = f"On {_scenario_phrase(sc_name)}, {label} = {_fmt(value)}{_unit_suffix(unit)}"
                    if check is not None:
                        fact["limit"] = float(check["limit"])
                        fact["op"] = check["op"]
                        fact["pass"] = bool(check["pass"])
                        text += (
                            f" (requirement {check['op']} {_fmt(check['limit'])}"
                            f"{_unit_suffix(unit)} — {'pass' if check['pass'] else 'FAIL'})"
                        )
                    lines.append(text + ".")
                    facts.append(fact)

        if wants_status or (not metrics and not scenarios):
            all_pass = bool(summary.get("all_constraints_pass"))
            n_pass = summary.get("n_scenarios_pass")
            n_scen = summary.get("n_scenarios")
            facts.append(
                {
                    "metric": "n_scenarios_pass",
                    "value": None if n_pass is None else float(n_pass),
                    "source": "scorecard",
                }
            )
            facts.append(
                {"metric": "n_scenarios", "value": None if n_scen is None else float(n_scen), "source": "scorecard"}
            )
            verdict = "meets every hard requirement" if all_pass else "does NOT meet every hard requirement"
            lines.append(
                f"The current controller {verdict}: {_fmt(n_pass)} of {_fmt(n_scen)} tests passed."
            )
            cert = job.certification
            if cert is not None:
                lines.append(
                    "Certification gate: "
                    + ("ALLOW — export is permitted." if cert.get("allowed") else "BLOCK — export is refused.")
                )

        if not metrics and (scenarios or wants_all):
            # Per-scenario overview (score + pass/fail); numbers all from scorecard.
            target = available if (wants_all or not scenarios) else scenarios
            for sc_name in target:
                item = next((i for i in sc["scenarios"] if i["name"] == sc_name), None)
                if item is None:
                    continue
                passed = bool(item.get("constraints", {}).get("all_pass"))
                score = item.get("scalar_score")
                facts.append(
                    {"scenario": sc_name, "metric": "scalar_score", "value": None if score is None else float(score), "source": "scorecard"}
                )
                lines.append(
                    f"{_scenario_phrase(sc_name).capitalize()}: "
                    f"{'passed' if passed else 'failed'} (score {_fmt(score)}, lower is better)."
                )

        if not lines:
            askable = ", ".join(sorted({_METRIC_LABEL[m] for m in _METRIC_LABEL if any(
                m in item.get("metrics", {}) for item in sc["scenarios"]
            )}))
            lines.append(
                "I can report exact numbers from the scorecard. Try asking about: "
                f"{askable}; or pass/fail on any test."
            )

        return {"grounded": True, "answer": " ".join(lines), "facts": facts}

    def modify(self, change: str) -> dict[str, Any]:
        """Apply a requested change to the spec (relax settling, add a test, …)."""
        from saas.feedback import apply_user_feedback

        spec = self._require_spec()
        summary = None if self.job.scorecard is None else self.job.scorecard.get("summary")
        updated, plan = apply_user_feedback(spec, change, use_llm=False, scorecard_summary=summary)
        self.job._spec = updated
        self.job.spec_dict = updated.to_dict()
        self.job.touch()
        return {
            "action": plan.get("action"),
            "reason": plan.get("reason"),
            "spec": updated.to_dict(),
            "note": "Spec updated. Call design_controller again to redesign under the new spec.",
        }

    def export(self) -> dict[str, Any]:
        """Certification gate + export package (raises/blocks if constraints fail)."""
        from saas.service import export_job

        try:
            path = export_job(self.job)
        except (RuntimeError, PermissionError) as exc:
            return {"exported": False, "error": str(exc), "certification": self.job.certification}
        allowed = bool((self.job.certification or {}).get("allowed"))
        return {
            "exported": allowed and str(path).endswith(".zip"),
            "path": str(path),
            "certification": self.job.certification,
        }

    # ------------------------------------------------------------------ #
    # Summaries
    # ------------------------------------------------------------------ #
    def _design_summary(self, controller_type: str) -> dict[str, Any]:
        job = self.job
        sc = job.scorecard or {}
        summary = sc.get("summary", {})
        cert = job.certification or {}
        return {
            "controller_type": controller_type,
            "controller": sc.get("controller"),
            "all_constraints_pass": summary.get("all_constraints_pass"),
            "n_scenarios_pass": summary.get("n_scenarios_pass"),
            "n_scenarios": summary.get("n_scenarios"),
            "mean_scalar_score": summary.get("mean_scalar_score"),
            "worst_case_ITAE": summary.get("worst_case_ITAE"),
            "certified": cert.get("allowed"),
            "session_status": None if job._session is None else job._session.status,
        }

    # ================================================================== #
    # OpenAI function-calling loop
    # ================================================================== #
    def chat(self, user_message: str, *, max_tool_rounds: int = 8) -> str:
        """Process one user turn through the OpenAI tool-calling loop.

        OpenAI-only (no silent heuristic fallback): if the key is missing a clear
        message is printed and RuntimeError raised — consistent with the rest of the
        project. The *tools* remain callable deterministically for tests / fallback.
        """
        key = os.getenv("OPENAI_API_KEY")
        if not key or key.startswith("sk-your-key"):
            msg = llm_unavailable_message(detail="OPENAI_API_KEY missing for design agent chat.")
            print(msg)
            raise RuntimeError(msg)

        from openai import OpenAI

        client = OpenAI(api_key=key)
        model_name = self.model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)

        if not self.messages:
            self.messages.append({"role": "system", "content": _system_prompt()})
        self.messages.append({"role": "user", "content": user_message})
        self.job.chat.append({"role": "user", "content": user_message})

        for _ in range(max_tool_rounds):
            response = client.chat.completions.create(
                model=model_name,
                temperature=0.0,
                messages=self.messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
            )
            usage = response.usage
            self.total_tokens += int(getattr(usage, "total_tokens", 0) or 0)
            msg = response.choices[0].message

            if not msg.tool_calls:
                content = msg.content or ""
                self.messages.append({"role": "assistant", "content": content})
                self.job.chat.append({"role": "assistant", "content": content})
                self.job.touch()
                return content

            # Record the assistant turn that requested tool calls.
            self.messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                result = self._dispatch_tool(tc.function.name, tc.function.arguments)
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": json.dumps(result, default=str),
                    }
                )

        # Tool-call budget exhausted — ask the model to summarize with no more tools.
        response = client.chat.completions.create(
            model=model_name,
            temperature=0.0,
            messages=self.messages
            + [{"role": "user", "content": "Summarize the result for the engineer now, citing only tool numbers."}],
        )
        content = response.choices[0].message.content or ""
        self.messages.append({"role": "assistant", "content": content})
        self.job.chat.append({"role": "assistant", "content": content})
        self.job.touch()
        return content

    def _dispatch_tool(self, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
        try:
            args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return {"error": f"Could not parse tool arguments: {exc}"}

        try:
            result = self._call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 — surface tool errors to the model
            result = {"error": f"{type(exc).__name__}: {exc}"}
        self.tool_log.append({"tool": name, "args": args, "result": result})
        return result

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "define_plant":
            return self.define_plant(
                description=args.get("description"),
                J=args.get("J"),
                b=args.get("b"),
                K=args.get("K"),
                R=args.get("R"),
                L=args.get("L"),
                V_max=args.get("V_max"),
                name=args.get("name"),
            )
        if name == "set_spec":
            return self.set_spec(args["text"])
        if name == "check_feasibility":
            return self.check_feasibility()
        if name == "design_controller":
            return self.design_controller(
                controller_type=args.get("controller_type", "auto"),
                mode=args.get("mode", "heuristic"),
                max_iterations=args.get("max_iterations"),
            )
        if name == "simulate":
            return self.simulate()
        if name == "query_results":
            return self.query_results(args.get("question", ""))
        if name == "modify":
            return self.modify(args.get("change", ""))
        if name == "export":
            return self.export()
        return {"error": f"Unknown tool {name!r}."}


def _scenario_phrase(name: str) -> str:
    return _SCENARIO_PHRASE.get(name, name.replace("_", " "))


def _unit_suffix(unit: str) -> str:
    if not unit:
        return ""
    return unit if unit == "%" else f" {unit}"


# --------------------------------------------------------------------------- #
# OpenAI tool schemas + system prompt
# --------------------------------------------------------------------------- #
def _system_prompt() -> str:
    return (
        "You are the Control Design Copilot for a DC motor SPEED controller "
        "(simulation only — never claim hardware readiness).\n\n"
        "You hold ONE always-on conversation with an engineer and drive a deterministic "
        "engine through tools. Absolute rules:\n"
        "- Tools COMPUTE every number. You NEVER invent gains, metrics, or pass/fail.\n"
        "- For ANY question about results (\"what was the settling time?\") you MUST call "
        "query_results and quote only the numbers it returns.\n"
        "- Workflow: define_plant -> set_spec -> check_feasibility -> (push back in plain "
        "language if infeasible until the spec is physically achievable) -> let the engineer "
        "pick a controller type -> design_controller -> report results -> modify/redesign on "
        "request -> export (a certification package, gated by simulation results).\n"
        "- If the engineer describes a motor, call define_plant. If they state performance "
        "goals, call set_spec. Only ask a clarifying question when you are genuinely blocked.\n"
        f"- Controller types you can design: {list(CONTROLLER_TYPES)}.\n"
        "Keep replies short and engineer-friendly."
    )


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "define_plant",
            "description": (
                "Define the DC motor to control. Provide either a natural-language "
                "'description' OR all five numeric parameters J, b, K, R, L (SI units) "
                "plus optional V_max and name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Plain-language motor description."},
                    "J": {"type": "number", "description": "Rotor inertia [kg·m^2]."},
                    "b": {"type": "number", "description": "Viscous friction [N·m·s/rad]."},
                    "K": {"type": "number", "description": "Torque/back-emf constant [N·m/A]."},
                    "R": {"type": "number", "description": "Armature resistance [ohm]."},
                    "L": {"type": "number", "description": "Armature inductance [H]."},
                    "V_max": {"type": "number", "description": "Voltage budget [V] (default 12)."},
                    "name": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_spec",
            "description": "Interpret natural-language performance requirements into a validated DesignSpec.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "The engineer's performance goals."}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_feasibility",
            "description": "Deterministic physics check: is the current spec achievable on the current motor?",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "design_controller",
            "description": (
                "Design and score a controller of the chosen type. 'auto' runs the adaptive "
                "orchestrator (may switch topology); pid/robust/mpc/adaptive design that family."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "controller_type": {"type": "string", "enum": list(CONTROLLER_TYPES)},
                    "mode": {
                        "type": "string",
                        "enum": ["script", "heuristic", "llm"],
                        "description": "Only used when controller_type='auto'.",
                    },
                    "max_iterations": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simulate",
            "description": "Re-run the deterministic simulation of the current controller under the current spec.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_results",
            "description": (
                "Answer a question about the design results using ONLY numbers stored in the "
                "scorecard. Use this for every numeric question about performance."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify",
            "description": "Apply a requested change to the spec (e.g. 'relax settling to 2.5 s', 'add a load disturbance').",
            "parameters": {
                "type": "object",
                "properties": {"change": {"type": "string"}},
                "required": ["change"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export",
            "description": "Run the certification gate and export the package. Blocks if hard constraints fail.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


__all__ = [
    "CONTROLLER_TYPES",
    "DesignAgentSession",
    "TOOL_SCHEMAS",
    "scorecard_numbers",
]
