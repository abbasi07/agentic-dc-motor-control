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
from dc_motor.specs import (
    DesignSpec,
    build_disclosures,
    design_spec_from_dict,
    reconcile_spec_with_plant,
    spec_sanity_advisories,
)

from .certify import certify_candidate
from .controller_registry import CONTROLLER_TYPE_NAMES, design_by_type
from .design_candidate import DesignCandidate, candidate_from_controller
from .domain_guard import refusal_message, should_refuse
from .orchestrator import DesignSession, grounded_rationale
from .spec_agent import DEFAULT_MODEL, interpret_spec, llm_unavailable_message
from .workflow import build_workspace, compute_phase

load_dotenv()

# Controller families a user can explicitly pick (design_controller(type=...)),
# sourced from the pluggable controller registry ("auto" runs the orchestrator).
CONTROLLER_TYPES = CONTROLLER_TYPE_NAMES


def _spec_adjustments(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable diff of what the engine changed vs. what was requested.

    Surfaces clamps / horizon derivations / scenario edits so the agent can report
    them verbatim instead of silently presenting rewritten numbers.
    """
    adjustments: list[str] = []

    b_hc = before.get("hard_constraints", {}) or {}
    a_hc = after.get("hard_constraints", {}) or {}
    for metric in sorted(set(b_hc) | set(a_hc)):
        bv = b_hc.get(metric)
        av = a_hc.get(metric)
        if bv != av:
            adjustments.append(f"{metric}: {bv} -> {av}")

    if before.get("t_final") != after.get("t_final"):
        adjustments.append(
            f"t_final: {before.get('t_final')} -> {after.get('t_final')} s"
        )
    if before.get("required_scenarios") != after.get("required_scenarios"):
        adjustments.append(
            f"scenarios: {before.get('required_scenarios')} -> "
            f"{after.get('required_scenarios')}"
        )

    # Any brand-new warnings (clamps, derivations, drops) introduced by this change.
    new_warnings = [w for w in after.get("warnings", []) if w not in set(before.get("warnings", []))]
    adjustments.extend(new_warnings)
    return adjustments

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

# Live-event types (E2.4). Kept as literals so agents/ stays decoupled from saas/;
# they mirror the canonical enum in saas.events. The event *bus* is duck-typed
# (anything exposing ``publish(job_id, type, data)``) and injected by the service layer.
_EVT_MESSAGE_DELTA = "message.delta"
_EVT_TOOL_STARTED = "tool.started"
_EVT_TOOL_FINISHED = "tool.finished"
_EVT_REFUSAL = "refusal"
_EVT_WORKSPACE_UPDATED = "workspace.updated"


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
    # Optional live-event bus (E2.4), injected by the service layer. Duck-typed:
    # anything with ``publish(job_id, type, data)``. ``None`` => events are a no-op.
    # Not part of snapshot()/restore() (it is transport, not durable state).
    events: Any = field(default=None, repr=False)

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
    # Persistence (E2.2): snapshot / restore the tool-calling transcript so the
    # session survives a restart and can be rehydrated in the worker process.
    # The job (artifacts) is persisted separately; here we only carry the OpenAI
    # transcript + audit log + token tally.
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        return {
            "messages": list(self.messages),
            "tool_log": list(self.tool_log),
            "total_tokens": int(self.total_tokens),
            "model": self.model,
        }

    def restore(self, state: dict[str, Any] | None) -> "DesignAgentSession":
        if state:
            self.messages = list(state.get("messages", []))
            self.tool_log = list(state.get("tool_log", []))
            self.total_tokens = int(state.get("total_tokens", 0) or 0)
            if state.get("model") and self.model is None:
                self.model = state["model"]
        return self

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

    def _plant_v_max(self) -> float | None:
        """Actuator budget from the chat-defined motor, if any."""
        job = self.job
        if job._motor is not None:
            return float(job._motor.V_max)
        if job.motor_dict and job.motor_dict.get("V_max") is not None:
            return float(job.motor_dict["V_max"])
        return None

    def _store_reconciled_spec(self, spec: DesignSpec) -> DesignSpec:
        """Persist DesignSpec after aligning Operating Point with plant + NL text."""
        reconciled = reconcile_spec_with_plant(spec, plant_V_max=self._plant_v_max())
        job = self.job
        job._spec = reconciled
        job.spec_dict = reconciled.to_dict()
        return reconciled

    # ------------------------------------------------------------------ #
    # Deterministic helper: inject a spec without the LLM (tests / fallback)
    # ------------------------------------------------------------------ #
    def load_spec(self, spec: DesignSpec) -> None:
        reconciled = reconcile_spec_with_plant(spec, plant_V_max=self._plant_v_max())
        self.job._spec = reconciled
        self.job.spec_dict = reconciled.to_dict()
        if not self.job.nl_spec:
            self.job.nl_spec = reconciled.raw_spec

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
        """Define or revise the DC motor — NL, full numbers, or a partial update."""
        from saas.service import effective_motor_params, set_motor_from_params, set_motor_from_text

        numeric = {"J": J, "b": b, "K": K, "R": R, "L": L}
        has_numbers = all(v is not None for v in numeric.values())
        provided = {k: float(v) for k, v in numeric.items() if v is not None}
        has_existing = self.job._motor is not None or self.job.motor_dict is not None
        partial_update = (
            not has_numbers
            and has_existing
            and (bool(provided) or V_max is not None or name is not None)
        )

        if has_numbers:
            payload = {k: float(v) for k, v in numeric.items()}
            payload["V_max"] = float(V_max) if V_max is not None else 12.0
            payload["name"] = name or "custom_dc_motor"
            # append_chat=False: this agent will write one confirmation reply itself.
            set_motor_from_params(self.job, payload, append_chat=False)
        elif partial_update:
            # Revise any subset of params / V_max / name on the current motor.
            base = dict(effective_motor_params(self.job).__dict__)
            base.update(provided)
            if V_max is not None:
                base["V_max"] = float(V_max)
            else:
                base["V_max"] = float(self._plant_v_max() or 12.0)
            if name is not None:
                base["name"] = name
            elif self.job.motor_dict and self.job.motor_dict.get("name"):
                base["name"] = self.job.motor_dict["name"]
            else:
                base["name"] = "custom_dc_motor"
            set_motor_from_params(self.job, base, append_chat=False)
        elif description:
            # NL -> validated MotorModel (OpenAI-only, re-validated by physics).
            # User turn is already on job.chat from chat(); don't echo description again.
            set_motor_from_text(
                self.job, description, append_user=False, append_chat=False
            )
        else:
            return {
                "error": (
                    "Provide either a text description, all of J, b, K, R, L, "
                    "or (when a motor already exists) any subset to revise — "
                    "e.g. only V_max."
                )
            }

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
            "note": (
                "Motor (re)defined. Present the updated params/characteristics and ask "
                "the engineer to approve; call confirm(stage='motor') only after they agree. "
                "Prior motor/spec confirmations and design results were cleared."
            ),
        }

    def set_spec(self, text: str) -> dict[str, Any]:
        """Interpret natural-language performance goals into a validated DesignSpec."""
        spec = interpret_spec(text, model=self.model, plant_V_max=self._plant_v_max())
        job = self.job
        job.nl_spec = text.strip()
        # Inherit plant V_max and convert RPM→rad/s so Operating Point matches chat.
        spec = self._store_reconciled_spec(spec)
        job.confirmed = False
        job.spec_confirmed = False  # a (re)interpreted spec must be re-agreed
        feas = self.check_feasibility()
        return {
            "spec": spec.to_dict(),
            "feasibility": feas,
            "notes": spec.notes,
            "warnings": list(spec.warnings),
            # Alias so the model consistently sees clamps/derivations/drops to report.
            "adjustments": list(spec.warnings),
            # Per-value source tags + the disclosures the agent MUST relay verbatim.
            "provenance": dict(spec.provenance),
            "disclosures": build_disclosures(spec),
            "sanity_advisories": spec_sanity_advisories(spec),
        }

    def confirm(self, stage: str) -> dict[str, Any]:
        """Record the engineer's explicit agreement to the motor or the spec.

        This is a negotiation gate: the workflow only advances past a stage once the
        engineer has confirmed it. Called by the LLM when the user clearly approves.
        """
        stage = (stage or "").lower().strip()
        job = self.job
        if stage in ("motor", "plant"):
            if job.motor_dict is None and job._motor is None:
                return {"error": "No motor to confirm yet. Define the motor first."}
            job.motor_confirmed = True
            # If a draft spec already exists, re-align its voltage budget with the plant.
            if job._spec is not None or job.spec_dict is not None:
                try:
                    self._store_reconciled_spec(self._require_spec())
                    self.check_feasibility()
                except RuntimeError:
                    pass
            job.touch()
            return {"confirmed": "motor", "phase": self.phase()}
        if stage in ("spec", "specs", "requirements", "requirement"):
            if job.spec_dict is None and job._spec is None:
                return {"error": "No requirements to confirm yet. Set the spec first."}
            # Final reconcile before locking requirements (plant may have changed).
            self._store_reconciled_spec(self._require_spec())
            job.spec_confirmed = True
            job.touch()
            return {"confirmed": "spec", "phase": self.phase()}
        return {"error": "Unknown stage. Use 'motor' or 'spec'."}

    # ------------------------------------------------------------------ #
    # Live events (E2.4) — best-effort; never break a chat turn
    # ------------------------------------------------------------------ #
    def _emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        bus = self.events
        if bus is None:
            return
        try:
            bus.publish(getattr(self.job, "job_id", None), event_type, data or {})
        except Exception:  # noqa: BLE001 - event publishing is best-effort by contract
            pass

    def _emit_workspace(self) -> None:
        """Emit the current reflect-only workspace snapshot (numbers all tool-computed)."""
        if self.events is None:
            return
        try:
            snapshot = self.workspace()
        except Exception:  # noqa: BLE001
            return
        self._emit(_EVT_WORKSPACE_UPDATED, snapshot)

    # ------------------------------------------------------------------ #
    # Workflow projection (reflect-only)
    # ------------------------------------------------------------------ #
    def phase(self) -> str:
        return compute_phase(self.job)

    def workspace(self) -> dict[str, Any]:
        """Reflect-only workspace snapshot for the frontend (see agents/workflow.py)."""
        # Pull budget limits from the service layer (kept lazy so agents/ does not import
        # saas.config directly); best-effort so the agent works even standalone in tests.
        limits = None
        try:
            from saas.service import budget_limits

            limits = budget_limits()
        except Exception:  # noqa: BLE001 - workspace must never fail on config lookup
            limits = None
        return build_workspace(self.job, session=self, limits=limits)

    def check_feasibility(self) -> dict[str, Any]:
        """Physics-based feasibility of the current spec on the current motor."""
        from saas.service import effective_motor_params

        # Keep Operating Point voltage in sync with the plant before analyzing.
        spec = self._store_reconciled_spec(self._require_spec())
        params = effective_motor_params(self.job)
        report = check_feasibility(params, spec, V_max=self._plant_v_max())
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
        # Dispatch to the pluggable controller registry (pid/robust/lqr/lqg/mpc/
        # mrac/fuzzy + aliases). "auto" is handled by the orchestrator upstream.
        return design_by_type(
            controller_type, spec, base_params=base_params, plant_factory=plant_factory
        )

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

        plant_redirect = _plant_change_redirect(change)
        if plant_redirect is not None:
            return plant_redirect

        try:
            spec = self._require_spec()
        except RuntimeError as exc:
            return {"error": str(exc)}
        summary = None if self.job.scorecard is None else self.job.scorecard.get("summary")
        before = spec.to_dict()
        updated, plan = apply_user_feedback(spec, change, use_llm=False, scorecard_summary=summary)
        # Persist through the reconciler so plant voltage / RPM stay aligned.
        updated = self._store_reconciled_spec(updated)
        # A changed spec must be re-agreed before any (re)design can run. This is the
        # gate that stops the app from silently jumping back into the Design phase.
        self.job.confirmed = False
        self.job.spec_confirmed = False
        self.job.touch()
        self._emit_workspace()
        return {
            "action": plan.get("action"),
            "reason": plan.get("reason"),
            "spec": updated.to_dict(),
            "changes": plan.get("changes", []),
            "adjustments": _spec_adjustments(before, updated.to_dict()),
            "provenance": dict(updated.provenance),
            "disclosures": build_disclosures(updated),
            "sanity_advisories": spec_sanity_advisories(updated),
            "note": (
                "Spec updated. Report the new values plus any adjustments/disclosures/"
                "advisories to the engineer and get their approval: call confirm(stage='spec'), "
                "then ask which controller family they want before design_controller."
            ),
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

        Domain guard: an obviously off-topic turn is refused deterministically (no
        model call), keeping the copilot locked to DC-motor controller design.
        """
        in_progress = self.job.motor_dict is not None or self.job.spec_dict is not None
        if should_refuse(user_message, in_progress=in_progress):
            reply = refusal_message()
            self.job.chat.append({"role": "user", "content": user_message})
            self.job.chat.append({"role": "assistant", "content": reply})
            self.job.touch()
            self._emit(_EVT_REFUSAL, {"content": reply})
            return reply

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
                self._emit(_EVT_MESSAGE_DELTA, {"content": content, "final": True})
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
        self._emit(_EVT_MESSAGE_DELTA, {"content": content, "final": True})
        return content

    def _dispatch_tool(self, name: str, arguments: str | dict[str, Any]) -> dict[str, Any]:
        try:
            args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return {"error": f"Could not parse tool arguments: {exc}"}

        self._emit(_EVT_TOOL_STARTED, {"tool": name, "args": args})
        try:
            result = self._call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 — surface tool errors to the model
            result = {"error": f"{type(exc).__name__}: {exc}"}
        self.tool_log.append({"tool": name, "args": args, "result": result})
        self._emit(_EVT_TOOL_FINISHED, {"tool": name, "result": result})
        # A tool may have mutated durable state (motor/spec/scorecard/…) — reflect it.
        self._emit_workspace()
        return result

    def _gated_design_controller(self, args: dict[str, Any]) -> dict[str, Any]:
        """Hard phase gate for the LLM-driven design step.

        The chat model can only reach a real design run after the motor AND the
        spec are explicitly confirmed and a controller family has been explicitly
        chosen. This makes the workflow deterministic regardless of model quality:
        a weak model cannot skip straight to Design or silently default to 'auto'.
        """
        job = self.job
        if not getattr(job, "motor_confirmed", False):
            return {
                "error": "MOTOR_NOT_CONFIRMED",
                "message": "The motor is not confirmed yet. Present the plant and call "
                "confirm(stage='motor') after the engineer approves, before designing.",
                "phase": self.phase(),
            }
        if self.job._spec is None and self.job.spec_dict is None:
            return {
                "error": "NO_SPEC",
                "message": "No performance spec yet. Call set_spec, review feasibility, "
                "then confirm(stage='spec').",
                "phase": self.phase(),
            }
        if not getattr(job, "spec_confirmed", False):
            return {
                "error": "SPEC_NOT_CONFIRMED",
                "message": "The requirements are not confirmed. Show the engineer the "
                "current spec (including any adjustments) and call confirm(stage='spec') "
                "once they approve — do not design against an unconfirmed spec.",
                "phase": self.phase(),
            }
        controller_type = args.get("controller_type")
        if controller_type is None or str(controller_type).strip() == "":
            return {
                "error": "CONTROLLER_TYPE_REQUIRED",
                "message": "Ask the engineer which controller family they want and pass it "
                "explicitly. Never default to a family on their behalf.",
                "choices": list(CONTROLLER_TYPES),
                "phase": self.phase(),
            }
        if str(controller_type).lower().strip() not in CONTROLLER_TYPES:
            return {
                "error": "UNKNOWN_CONTROLLER_TYPE",
                "message": f"Unknown controller_type {controller_type!r}.",
                "choices": list(CONTROLLER_TYPES),
            }
        return self.design_controller(
            controller_type=str(controller_type),
            mode=args.get("mode", "heuristic"),
            max_iterations=args.get("max_iterations"),
        )

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
        if name == "confirm":
            return self.confirm(args.get("stage", ""))
        if name == "design_controller":
            return self._gated_design_controller(args)
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


_PLANT_CHANGE_MARKERS: tuple[str, ...] = (
    "v_max",
    "vmax",
    "v max",
    "max voltage",
    "supply voltage",
    "bus voltage",
    "double the voltage",
    "double voltage",
    "change the voltage",
    "change voltage",
    "update the motor",
    "change the motor",
    "redefine the motor",
    "motor parameter",
    "motor params",
    "plant parameter",
    "plant params",
    "viscous friction",
    "rotor inertia",
    "armature resistance",
    "armature inductance",
)


def _plant_change_redirect(change: str) -> dict[str, Any] | None:
    """If feedback is about the plant, steer the LLM to define_plant (not modify)."""
    t = (change or "").lower().strip()
    if not t:
        return None
    looks_plant = any(m in t for m in _PLANT_CHANGE_MARKERS)
    looks_plant = looks_plant or bool(
        re.search(r"\b([jbkr]|l)\s*=\s*[-+]?\d", t)
        or re.search(r"\b(j|b|k|r|l)\b.{0,12}\b(to|=)\b", t)
    )
    if not looks_plant:
        return None
    return {
        "error": (
            "That request changes the motor/plant (e.g. J, b, K, R, L, or V_max). "
            "Do NOT use modify for plant edits. Call define_plant again with the "
            "updated values — partial updates are allowed when a motor already exists "
            "(e.g. only V_max). No performance spec is required to revise the motor."
        ),
        "use_tool": "define_plant",
    }


# --------------------------------------------------------------------------- #
# OpenAI tool schemas + system prompt
# --------------------------------------------------------------------------- #
def _system_prompt() -> str:
    return (
        "You are the Control Design Copilot for a DC motor SPEED controller "
        "(simulation only — never claim hardware readiness).\n\n"
        "SCOPE LOCK: You ONLY help design, test, and certify controllers for DC motors. "
        "You are NOT a general assistant. If the user asks for anything unrelated "
        "(jokes, code unrelated to control, general knowledge, etc.), politely decline in "
        "one sentence and steer them back to motor/controller design. Do not answer it.\n\n"
        "You hold ONE always-on conversation with an engineer and drive a deterministic "
        "engine through tools. Absolute rules:\n"
        "- Tools COMPUTE every number. You NEVER fabricate gains, metrics, feasibility, or "
        "pass/fail. Every RESULT number you state MUST come from a tool result this session.\n"
        "- You MAY interpret and, when asked, propose values — but you must be transparent "
        "about their SOURCE. set_spec/modify return a 'provenance' map plus 'disclosures', "
        "'adjustments'/'warnings', and 'sanity_advisories'. After EVERY set_spec/modify you "
        "MUST relay, in plain language:\n"
        "   (1) DISCLOSURES — any value you or the engine supplied that the engineer did not "
        "state (assumed / default / derived / clamped). Say it was assumed and ask them to "
        "confirm or change it. Never present an assumed value as if they specified it.\n"
        "   (2) MISSING inputs — if a needed value (e.g. target speed) is absent, ask for it.\n"
        "   (3) SANITY ADVISORIES + feasibility issues — when a number is unrealistic or "
        "conflicting, explain WHY (use the tool's message) and offer the suggested realistic "
        "range. Do not silently accept it.\n"
        "- DELEGATION: if the engineer asks you to choose a value ('you pick', 'whatever is "
        "suitable'), propose one WITH a brief reason, mark that you chose it, and get their "
        "explicit approval (they must confirm) before it is locked. Never lock in a value "
        "you selected without approval.\n"
        "- When you call set_spec, pass the engineer's OWN wording (do not substitute numbers "
        "you invented into the text) so the source of each value stays accurate.\n"
        "- For ANY question about results (\"what was the settling time?\") you MUST call "
        "query_results and quote only the numbers it returns.\n\n"
        "WORKFLOW (guide the engineer stage-by-stage, but stages are REVISABLE — never treat "
        "the flow as one-way):\n"
        "1. MOTOR: When the engineer describes a motor OR asks to change motor params / "
        "V_max at ANY time (before or after confirmation, even mid-design), call "
        "define_plant. Partial updates are OK once a motor exists (e.g. only V_max=36). "
        "Then write ONE reply that (a) lists the proposed params (J, b, K, R, L, V_max), "
        "(b) cites the tool's derived characteristics (τ_mech, τ_elec, ω_max), and (c) notes "
        "any warnings. Ask them to approve or say what to change. Do NOT ask for performance "
        "goals yet unless the motor is already confirmed and they are ready. If numbers look "
        "non-physical, push back. When they clearly approve, call confirm(stage='motor'). "
        "NEVER use modify for plant/V_max changes — modify is for performance requirements "
        "only. Redefining the motor clears confirmations and stale design results; that is "
        "expected — present the new plant and continue.\n"
        "2. SPEC: Only after the motor is confirmed, ask for performance goals; call "
        "set_spec, then check_feasibility. set_spec inherits the plant's V_max into the "
        "Operating Point and converts RPM→rad/s automatically. Report the resulting spec, "
        "then explicitly walk through the tool's 'disclosures' (assumed/derived/default/"
        "clamped values), 'sanity_advisories', and any feasibility issues — with the reasons "
        "and suggested realistic ranges. Get the engineer to confirm or change every assumed "
        "value. If the spec is infeasible or tight, negotiate until achievable. Only when the "
        "engineer approves the FULL spec (including anything you assumed) call confirm(stage='spec').\n"
        "3. CONTROLLER: Only after the spec is confirmed, ASK which controller family they "
        f"want ({list(CONTROLLER_TYPES)}) — 'auto' runs the adaptive orchestrator. Wait for "
        "their explicit choice, ask any needed clarifying questions, then call "
        "design_controller with that exact controller_type. NEVER choose a family yourself "
        "or default to 'auto' without them saying so.\n"
        "4. RESULTS: Report the outcome grounded in query_results. Offer to modify/redesign. "
        "After ANY modify (spec), the spec becomes UNCONFIRMED again: report the changes, get "
        "the engineer's approval, call confirm(stage='spec'), re-ask the controller family, "
        "THEN design_controller. Do not chain modify straight into design. If they change "
        "the motor instead, go back to step 1 via define_plant.\n"
        "5. EXPORT: When the engineer is satisfied, call export (a certification package, "
        "gated by simulation results).\n\n"
        "Only ask a clarifying question when genuinely blocked. Keep replies short and "
        "engineer-friendly.\n\n"
        "FORMATTING (chat UI renders Markdown + KaTeX):\n"
        "- Prefer concise **bold** labels and short `-` bullet lists for parameters and "
        "results. Do not write long prose paragraphs.\n"
        "- Put math in KaTeX: inline `$J = 0.001$`, `$\\tau_{\\mathrm{mech}}$`, "
        "`$\\omega_{\\max}$`; display `$$...$$` only when a formula needs its own line.\n"
        "- Units may stay in plain text after the math (e.g. `$J = 0.001$ kg·m²).\n"
        "- Never wrap the whole reply in a code fence."
    )


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "define_plant",
            "description": (
                "Define or REVISE the DC motor at any time (before or after confirmation). "
                "Provide a natural-language 'description', OR all five of J, b, K, R, L "
                "(SI units) plus optional V_max/name, OR — when a motor already exists — "
                "any subset of those fields to update (e.g. only V_max). Never refuse a "
                "motor/V_max change because a performance spec is missing; use this tool "
                "instead of modify for plant edits."
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
            "name": "confirm",
            "description": (
                "Record the engineer's explicit agreement to advance the workflow. Call "
                "with stage='motor' once they approve the motor, or stage='spec' once they "
                "approve the requirements. Do NOT call this without clear user agreement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "enum": ["motor", "spec"]},
                },
                "required": ["stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "design_controller",
            "description": (
                "Design and score a controller. BLOCKED until the motor and spec are both "
                "confirmed and the engineer has explicitly chosen a controller family. "
                "'auto' runs the adaptive orchestrator (may switch topology); "
                "pid/robust/lqr/lqg/mpc/mrac/fuzzy design that family. Never call this with "
                "a family the engineer did not choose."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "controller_type": {
                        "type": "string",
                        "enum": list(CONTROLLER_TYPES),
                        "description": "The family the engineer explicitly chose. Required.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["script", "heuristic", "llm"],
                        "description": "Only used when controller_type='auto'.",
                    },
                    "max_iterations": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["controller_type"],
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
            "description": (
                "Apply a requested change to the PERFORMANCE SPEC only "
                "(e.g. 'relax settling to 2.5 s', 'add a load disturbance'). "
                "Do NOT use this for motor/plant edits (J, b, K, R, L, V_max) — "
                "call define_plant for those."
            ),
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
