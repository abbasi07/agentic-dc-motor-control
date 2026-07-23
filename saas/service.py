"""Job workflow helpers used by FastAPI and Streamlit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.certify import certify_candidate, export_certified_package
from agents.orchestrator import run_design_session
from agents.plant_agent import interpret_plant, motor_model_from_dict
from agents.spec_agent import interpret_spec, llm_unavailable_message
from dc_motor.evaluate import scorecard_to_json
from dc_motor.feasibility import check_feasibility
from dc_motor.motor_model import MotorModel
from dc_motor.plant import MotorParams
from dc_motor.registry import DEFAULT_PLANT_ID, get_plant_spec, list_plants, motor_params_for
from dc_motor.specs import DesignSpec, design_spec_from_dict

from .clarify import critique_design_spec
from .events import (
    EVENT_ERROR,
    EVENT_RUN_STATUS,
    EVENT_WORKSPACE_UPDATED,
    get_event_bus,
)
from .feedback import apply_user_feedback
from .jobs import DesignJob, JobStore, default_export_dir, get_job_store
from .present import design_finished_message, feedback_plan_message


def _save(job: DesignJob) -> DesignJob:
    """Persist mutated job state through the active store.

    No-op for the in-memory :class:`saas.jobs.JobStore`; serializes to Postgres when
    persistence is enabled so state survives restarts and reaches the worker process.
    """
    return get_job_store().save(job)


# --------------------------------------------------------------------------- #
# Live events (E2.4) — published by BOTH the API (inline) and the RQ worker so any
# connected SSE client sees run.status transitions + workspace snapshots. Best-effort:
# a no-op when events are disabled (host tools / tests) and never raises.
# --------------------------------------------------------------------------- #
def _publish_run_status(job: DesignJob) -> None:
    bus = get_event_bus()
    if bus is None:
        return
    bus.publish(
        job.job_id,
        EVENT_RUN_STATUS,
        {"status": job.status, "error": job.error, "queue_job_id": job.queue_job_id},
    )


def _publish_workspace(job: DesignJob) -> None:
    bus = get_event_bus()
    if bus is None:
        return
    bus.publish(job.job_id, EVENT_WORKSPACE_UPDATED, workspace_for_job(job))


def create_job(
    *, plant_id: str = DEFAULT_PLANT_ID, mode: str = "heuristic", tenant_id: str | None = None
) -> DesignJob:
    get_plant_spec(plant_id)  # validate
    return get_job_store().create(plant_id=plant_id, mode=mode, tenant_id=tenant_id)


# --------------------------------------------------------------------------- #
# Custom DC motor (chat-defined plant)
# --------------------------------------------------------------------------- #
def _record_motor(job: DesignJob, motor: MotorModel) -> DesignJob:
    job._motor = motor
    job.motor_dict = motor.to_dict()
    job.plant_id = "custom_dc_motor"
    # A (re)defined motor is a fresh proposal: it must be re-confirmed, and any
    # prior spec agreement is invalidated because feasibility depends on the motor.
    job.motor_confirmed = False
    job.spec_confirmed = False
    if motor.warnings:
        warn_md = "\n".join(f"- {w}" for w in motor.warnings)
        note = f"\n\nHeads-up on the numbers you gave:\n{warn_md}"
    else:
        note = ""
    chars = motor.to_dict()["characteristics"]
    job.chat.append(
        {
            "role": "assistant",
            "content": (
                f"Got it — I set up **{motor.name}** as the plant. "
                f"It reaches about **{chars['omega_max_rad_s']:.4g} rad/s** at ±{motor.V_max:g} V "
                f"(dominant time constant ≈ {chars['tau_mech_s']:.4g} s). "
                "Now tell me the performance you need." + note
            ),
        }
    )
    job.touch()
    return _save(job)


def set_motor_from_text(job: DesignJob, text: str, *, append_user: bool = True) -> DesignJob:
    """Interpret a natural-language DC-motor description into a custom plant."""
    if append_user:
        job.chat.append({"role": "user", "content": text.strip()})
    try:
        motor = interpret_plant(text)
    except RuntimeError as exc:
        job.error = str(exc)
        job.touch()
        raise
    return _record_motor(job, motor)


def set_motor_from_params(job: DesignJob, params: dict[str, Any]) -> DesignJob:
    """Set a custom plant from explicit numeric parameters (no LLM)."""
    motor = motor_model_from_dict(params, source="manual")
    return _record_motor(job, motor)


def effective_motor_params(job: DesignJob) -> MotorParams:
    """MotorParams the design run should use: custom motor if set, else the registry plant."""
    if job._motor is None and job.motor_dict is not None:
        job._motor = motor_model_from_dict(
            {**job.motor_dict.get("params", {}), "V_max": job.motor_dict.get("V_max", 12.0),
             "name": job.motor_dict.get("name", "custom_dc_motor")},
            source="manual",
        )
    if job._motor is not None:
        return job._motor.params
    return motor_params_for(job.plant_id)


def _feasibility_for_job(job: DesignJob, spec: DesignSpec) -> dict[str, Any]:
    params = effective_motor_params(job)
    report = check_feasibility(params, spec)
    job.feasibility = report.to_dict()
    return job.feasibility


def interpret_job_spec(
    job: DesignJob,
    nl_text: str,
    *,
    critique: bool = True,
    append_user: bool = True,
) -> DesignJob:
    job.nl_spec = nl_text.strip()
    if append_user:
        job.chat.append({"role": "user", "content": job.nl_spec})
    try:
        spec = interpret_spec(job.nl_spec)
    except RuntimeError as exc:
        job.status = "failed"
        job.error = str(exc)
        job.touch()
        _save(job)
        raise

    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.confirmed = False
    job.spec_confirmed = False  # a (re)interpreted spec must be re-agreed by the user

    # Physics-based feasibility against the (custom or registry) motor.
    feas = _feasibility_for_job(job, spec)
    feas_questions = [
        f"{i['message']} {i.get('suggestion', '')}".strip()
        for i in feas.get("issues", [])
        if i.get("severity") in {"error", "warning"}
    ]
    has_infeasible = not feas.get("feasible", True)

    if critique:
        critique_result = critique_design_spec(spec, plant_id=job.plant_id)
        questions = list(critique_result.get("questions", []))
        # Feasibility errors/warnings lead the list (most actionable).
        for q in feas_questions:
            if q not in questions:
                questions.insert(0, q)
        job.clarifying_questions = questions

        if has_infeasible or critique_result.get("needs_clarification"):
            job.status = "needs_clarification"
            q_md = "\n".join(f"- {q}" for q in job.clarifying_questions) or "- (none)"
            lead = (
                "These targets are **not physically achievable** on this motor as stated. "
                "Let's fix them before designing:"
                if has_infeasible
                else "I translated your goals into performance requirements, "
                "but a few points need clarifying before we design:"
            )
            job.chat.append({"role": "assistant", "content": f"{lead}\n{q_md}"})
        else:
            job.status = "spec_ready"
            ceiling = feas.get("characteristics", {}).get("omega_max_rad_s")
            extra = (
                f" (motor ceiling ≈ {float(ceiling):.4g} rad/s)" if isinstance(ceiling, (int, float)) else ""
            )
            job.chat.append(
                {
                    "role": "assistant",
                    "content": (
                        f"Your requirements look complete and feasible{extra}. "
                        "Review them in **Step 2**, then click **Design controller**."
                    ),
                }
            )
    else:
        job.status = "needs_clarification" if has_infeasible else "spec_ready"
        job.clarifying_questions = feas_questions if has_infeasible else []

    job.touch()
    return _save(job)


def answer_clarification(job: DesignJob, answer: str) -> DesignJob:
    """Append clarification and re-interpret combined NL + answer."""
    answer = answer.strip()
    job.chat.append({"role": "user", "content": answer})
    combined = (job.nl_spec + "\nClarification: " + answer).strip()
    return interpret_job_spec(job, combined, critique=True, append_user=False)


def confirm_and_run(
    job: DesignJob,
    *,
    max_iterations: int | None = None,
    maxiter_scipy: int = 8,
) -> DesignJob:
    _ensure_spec_for_run(job)

    job.confirmed = True
    job.status = "running"
    job.error = None
    job.touch()
    _save(job)  # persist "running" so status is visible while the worker computes
    _publish_run_status(job)  # queued/inline -> running (worker or API process)
    _publish_workspace(job)
    iters = max_iterations if max_iterations is not None else job.max_iterations

    # A chat-defined custom motor overrides the registry plant: pass MotorParams
    # directly (plant_id=None) so the engine simulates the user's actual motor.
    use_custom = job._motor is not None or job.motor_dict is not None
    run_kwargs: dict[str, Any] = dict(
        mode=job.mode,  # type: ignore[arg-type]
        max_iterations=iters,
        maxiter_scipy=maxiter_scipy,
        spec=job._spec,
    )
    if use_custom:
        run_kwargs["base_params"] = effective_motor_params(job)
        run_kwargs["plant_id"] = None
    else:
        run_kwargs["plant_id"] = job.plant_id

    try:
        session = run_design_session(
            job.nl_spec or job._spec.raw_spec,
            **run_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = str(exc)
        job.touch()
        _save(job)
        _publish_run_status(job)  # running -> failed
        bus = get_event_bus()
        if bus is not None:
            bus.publish(job.job_id, EVENT_ERROR, {"error": job.error, "where": "design_run"})
        raise

    job._session = session
    job._spec = session.spec
    job.spec_dict = session.spec.to_dict()
    job.session_dict = session.to_dict(include_scorecard_json=False)
    job.scorecard = None if session.best is None else session.best.scorecard
    if session.best is not None:
        cert = certify_candidate(session.best)
        job.certification = cert.to_dict()
    else:
        job.certification = None

    job.status = "completed"
    if session.best is not None:
        summary = design_finished_message(
            session.status,
            session.best.kind,
            bool(session.best.failure_digest.all_pass),
        )
    else:
        summary = (
            "Design finished without a usable controller candidate. "
            f"{session.status.replace('_', ' ')}."
        )
    job.chat.append({"role": "assistant", "content": summary})
    job.touch()
    saved = _save(job)
    _publish_run_status(job)  # running -> completed
    _publish_workspace(job)  # scorecard + plots now available to the client
    return saved


def _ensure_spec_for_run(job: DesignJob) -> None:
    """Rebuild the live DesignSpec (from persisted JSON if needed) or raise."""
    if job._spec is None and job.spec_dict is not None:
        job._spec = design_spec_from_dict(job.spec_dict, raw_spec=job.nl_spec, source="manual")
    if job._spec is None:
        raise RuntimeError("No DesignSpec to confirm. Interpret a natural-language spec first.")


def enqueue_design_run(
    job: DesignJob,
    *,
    max_iterations: int | None = None,
    queue: Any = None,
) -> DesignJob:
    """Enqueue a design run to the RQ worker and return immediately (E2.3).

    The job is persisted as ``queued`` so a poll reflects it right away; the worker
    rehydrates from the DB, runs :func:`confirm_and_run` (running -> completed/failed),
    and persists the result, which the API picks up via the ``rev`` rehydrate path.
    """
    _ensure_spec_for_run(job)
    from .queue import enqueue_design_run as _enqueue

    if max_iterations is not None:
        job.max_iterations = max_iterations  # persist override so the worker uses it
    job.confirmed = True
    job.status = "queued"
    job.error = None
    job.touch()
    _save(job)  # persist BEFORE enqueue so the worker sees committed state

    rq_job = _enqueue(job.job_id, max_iterations=max_iterations, queue=queue)
    job.queue_job_id = rq_job.id
    job.touch()
    saved = _save(job)
    # Tell any connected client the run is queued (the worker will emit running/completed).
    _publish_run_status(job)
    _publish_workspace(job)
    return saved


def run_status(job: DesignJob) -> dict[str, Any]:
    """Lightweight status/poll payload for an async design run."""
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
        "error": job.error,
        "queue_job_id": job.queue_job_id,
        "updated_at": job.updated_at,
    }
    if job.queue_job_id:
        try:
            from .queue import fetch_queue_job

            rq_job = fetch_queue_job(job.queue_job_id)
            payload["queue_state"] = None if rq_job is None else rq_job.get_status()
        except Exception:  # noqa: BLE001 - poll must never fail on queue introspection
            payload["queue_state"] = None
    return payload


def apply_feedback_and_maybe_rerun(
    job: DesignJob,
    feedback: str,
    *,
    use_llm: bool = False,
    rerun: bool = True,
) -> DesignJob:
    if job._spec is None and job.spec_dict is not None:
        job._spec = design_spec_from_dict(job.spec_dict, raw_spec=job.nl_spec, source="manual")
    if job._spec is None:
        raise RuntimeError("No DesignSpec available for feedback.")

    job.chat.append({"role": "user", "content": feedback})
    summary = None if job.scorecard is None else job.scorecard.get("summary")
    updated_spec, plan = apply_user_feedback(
        job._spec,
        feedback,
        use_llm=use_llm,
        scorecard_summary=summary,
    )
    job._spec = updated_spec
    job.spec_dict = updated_spec.to_dict()
    job.chat.append({"role": "assistant", "content": feedback_plan_message(plan)})

    action = plan["action"]

    # Actions that must NOT silently rerun the design loop.
    if action in {"accept", "unclear"}:
        job.touch()
        return _save(job)

    if action == "reinterpret_spec":
        return interpret_job_spec(job, feedback, critique=True, append_user=False)

    if rerun:
        # Prefer heuristic redesign after feedback; keep user's mode if llm
        return confirm_and_run(job)

    job.touch()
    return _save(job)


def export_job(job: DesignJob, *, out_dir: Path | None = None) -> Path:
    out = out_dir or default_export_dir()

    # Fast path: the live design session is still in this process (same-process run).
    if job._session is not None and job._session.best is not None:
        candidate = job._session.best
        rationale = job._session.rationale or "Exported from Design Copilot."
        action_trace = [a.to_dict() for a in job._session.action_trace]
    else:
        # Cross-process / post-restart path: rebuild an export-ready candidate stub from
        # the persisted scorecard + session_dict (no live controller needed — the export
        # writer only reads kind/params/scorecard + the controller name).
        from .serialization import rehydrated_candidate

        candidate = rehydrated_candidate(job)
        if candidate is None:
            raise RuntimeError("No design candidate to export.")
        session_dict = job.session_dict or {}
        rationale = session_dict.get("rationale") or "Exported from Design Copilot."
        action_trace = session_dict.get("action_trace") or []

    path = export_certified_package(
        candidate,
        rationale=rationale,
        out_dir=out,
        nl_spec=job.nl_spec,
        action_trace=action_trace,
    )
    job.export_path = str(path)
    job.status = "exported" if path.suffix == ".zip" else "completed"
    job.touch()
    _save(job)
    return path


def get_agent_session(job: DesignJob):
    """Return (creating if needed) the chat-first Design Agent bound to this job.

    After a rehydrate the live session is gone but ``job.agent_state`` holds the
    persisted transcript, so the tool-calling loop resumes exactly where it left off.
    """
    from agents.design_agent import DesignAgentSession

    if job._agent is None:
        job._agent = DesignAgentSession(job=job, model=None).restore(job.agent_state)
    # Attach the live-event bus (E2.4) so the chat loop can stream message.delta /
    # tool.started/finished / refusal / workspace.updated. ``None`` when disabled.
    job._agent.events = get_event_bus()
    return job._agent


def agent_chat(job: DesignJob, message: str) -> DesignJob:
    """Send one message to the tool-calling Design Agent (OpenAI-only chat loop)."""
    session = get_agent_session(job)
    try:
        session.chat(message)
    except RuntimeError as exc:
        job.error = str(exc)
        job.touch()
        _save(job)
        raise
    # Persist the updated transcript + any artifacts the tools produced this turn.
    job.agent_state = session.snapshot()
    job.touch()
    return _save(job)


def workspace_for_job(job: DesignJob) -> dict[str, Any]:
    """Reflect-only workspace snapshot (phase + artifacts) for the frontend."""
    from agents.workflow import build_workspace

    return build_workspace(job, session=job._agent)


def scorecard_json_for_job(job: DesignJob) -> str | None:
    if job.scorecard is None:
        return None
    return scorecard_to_json(job.scorecard)


def plants_public() -> list[dict[str, Any]]:
    return [p.to_dict() for p in list_plants()]


__all__ = [
    "agent_chat",
    "answer_clarification",
    "apply_feedback_and_maybe_rerun",
    "confirm_and_run",
    "create_job",
    "effective_motor_params",
    "enqueue_design_run",
    "export_job",
    "get_agent_session",
    "get_job_store",
    "interpret_job_spec",
    "llm_unavailable_message",
    "plants_public",
    "run_status",
    "scorecard_json_for_job",
    "set_motor_from_params",
    "set_motor_from_text",
    "workspace_for_job",
]
