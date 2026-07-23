"""FastAPI design-job API for the local Control Design Copilot."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from dc_motor.registry import DEFAULT_PLANT_ID

from . import service
from .jobs import get_job_store

load_dotenv()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """When persistence is enabled, ensure the DB schema + default tenant exist.

    Off by default (host tools / tests stay in-memory); Docker Compose sets
    ``COPILOT_PERSIST=true`` so the api + worker share Postgres. When auth is also on,
    seed the dev bootstrap API key (``COPILOT_DEV_API_KEY``) so local/demo use works
    with no signup.
    """
    from .config import get_settings

    settings = get_settings()
    if settings.persistence_enabled:
        from .repository import get_repository

        get_repository()
        if settings.auth_enabled:
            from .auth import get_auth_manager

            get_auth_manager().seed_dev_api_key()
    yield


# --------------------------------------------------------------------------- #
# Auth + tenant scoping (E2.5)
# --------------------------------------------------------------------------- #
def _bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <key>`` header."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def require_tenant(authorization: str | None = Header(default=None)) -> str:
    """Resolve the authenticated tenant for a request (FastAPI dependency).

    When auth is disabled (host tools / the OpenAI-free test-suite) every request is
    attributed to the dev tenant so nothing needs a key. When enabled, a valid
    ``Authorization: Bearer <key>`` is required (401 otherwise) and the tenant's Redis
    rate limit is enforced (429 when exceeded; fail-open if the broker is unreachable).
    """
    from .config import get_settings
    from .repository import DEFAULT_TENANT_ID

    settings = get_settings()
    if not settings.auth_enabled:
        return DEFAULT_TENANT_ID

    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Use 'Authorization: Bearer <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    from .auth import get_auth_manager

    tenant_id = get_auth_manager().verify_api_key(token)
    if tenant_id is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or inactive API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    _enforce_rate_limit(tenant_id, settings.rate_limit_per_minute)
    return tenant_id


def _enforce_rate_limit(tenant_id: str, limit: int) -> None:
    if limit <= 0:
        return
    from .ratelimit import get_rate_limiter

    result = get_rate_limiter().check(tenant_id, limit=limit)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit} requests/min). Slow down.",
            headers={"Retry-After": str(result.retry_after)},
        )


app = FastAPI(
    title="Control Design Copilot",
    description="Simulation-only adaptive controller design jobs (no hardware).",
    version="0.1.0",
    lifespan=_lifespan,
)


# CORS (E3): the React/Next UI runs on a different origin than the API, so the browser
# needs an explicit allow-list to send the Bearer key and read the SSE stream. Origins
# come from settings (COPILOT_CORS_ORIGINS); defaults to http://localhost:3000.
def _install_cors(fastapi_app: FastAPI) -> None:
    from fastapi.middleware.cors import CORSMiddleware

    from .config import get_settings

    origins = list(get_settings().cors_allow_origins)
    allow_all = "*" in origins
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else origins,
        # Credentials + wildcard origin are incompatible per the CORS spec; we only send
        # a Bearer header (not cookies), so disable credentials when allowing any origin.
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_install_cors(app)


class CreateJobRequest(BaseModel):
    plant_id: str = DEFAULT_PLANT_ID
    mode: Literal["script", "heuristic", "llm"] = "heuristic"
    max_iterations: int = Field(default=5, ge=1, le=20)


class InterpretRequest(BaseModel):
    text: str
    critique: bool = True


class MotorTextRequest(BaseModel):
    text: str


class MotorParamsRequest(BaseModel):
    J: float = Field(gt=0)
    b: float = Field(gt=0)
    K: float = Field(gt=0)
    R: float = Field(gt=0)
    L: float = Field(gt=0)
    V_max: float = Field(default=12.0, gt=0)
    name: str = "custom_dc_motor"


class ClarifyRequest(BaseModel):
    answer: str


class RunRequest(BaseModel):
    max_iterations: int | None = Field(default=None, ge=1, le=20)
    maxiter_scipy: int = Field(default=8, ge=1, le=50)


class FeedbackRequest(BaseModel):
    text: str
    use_llm: bool = False
    rerun: bool = True


class AgentChatRequest(BaseModel):
    message: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "scope": "simulation_only"}


@app.get("/plants")
def plants() -> dict[str, Any]:
    return {"plants": service.plants_public()}


@app.post("/jobs")
def create_job(
    body: CreateJobRequest, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    try:
        job = service.create_job(plant_id=body.plant_id, mode=body.mode, tenant_id=tenant)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job.max_iterations = body.max_iterations
    get_job_store().save(job)
    return job.to_public_dict()


@app.get("/jobs")
def list_jobs(tenant: str = Depends(require_tenant)) -> dict[str, Any]:
    jobs = [j.to_public_dict() for j in get_job_store().list_jobs(tenant_id=tenant)]
    return {"jobs": jobs}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, tenant: str = Depends(require_tenant)) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/motor")
def set_motor_text(
    job_id: str, body: MotorTextRequest, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        service.set_motor_from_text(job, body.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/motor/params")
def set_motor_params(
    job_id: str, body: MotorParamsRequest, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        service.set_motor_from_params(job, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/interpret")
def interpret(
    job_id: str, body: InterpretRequest, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        service.interpret_job_spec(job, body.text, critique=body.critique)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/clarify")
def clarify(
    job_id: str, body: ClarifyRequest, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        service.answer_clarification(job, body.answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/run")
def run_design(
    job_id: str,
    body: RunRequest | None = None,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Start a design run.

    When async runs are enabled (Compose default) the CPU-heavy design loop is enqueued
    to the RQ worker and this returns immediately with ``status="queued"`` — poll
    ``GET /jobs/{id}`` or ``GET /jobs/{id}/status`` for completion. Otherwise it runs
    inline (host tools / tests).
    """
    from .config import get_settings

    body = body or RunRequest()
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        if get_settings().async_runs_enabled:
            service.enqueue_design_run(job, max_iterations=body.max_iterations)
        else:
            service.confirm_and_run(
                job,
                max_iterations=body.max_iterations,
                maxiter_scipy=body.maxiter_scipy,
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return job.to_public_dict()


@app.get("/jobs/{job_id}/status")
def run_status(job_id: str, tenant: str = Depends(require_tenant)) -> dict[str, Any]:
    """Lightweight status/poll path for an (async) design run."""
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.run_status(job)


@app.post("/jobs/{job_id}/feedback")
def feedback(
    job_id: str, body: FeedbackRequest, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        service.apply_feedback_and_maybe_rerun(
            job, body.text, use_llm=body.use_llm, rerun=body.rerun
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/agent")
def agent_chat(
    job_id: str, body: AgentChatRequest, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    """Chat-first tool-calling Design Agent (workstream D). OpenAI-only.

    The agent owns the session and drives the deterministic engine through tools;
    every reported number comes from a tool, never from the model.
    """
    from .auth import BudgetExceeded

    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        service.agent_chat(job, body.message)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/export")
def export(job_id: str, tenant: str = Depends(require_tenant)) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
        path = service.export_job(job)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"export_path": str(path), "job": job.to_public_dict()}


@app.get("/jobs/{job_id}/export/download")
def download_export(job_id: str, tenant: str = Depends(require_tenant)):
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not job.export_path:
        raise HTTPException(status_code=404, detail="No export yet. POST /jobs/{id}/export first.")
    path = Path(job.export_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Export file missing: {path}")
    return FileResponse(path, filename=path.name)


@app.get("/jobs/{job_id}/workspace")
def workspace(job_id: str, tenant: str = Depends(require_tenant)) -> dict[str, Any]:
    """Reflect-only workspace snapshot: workflow phase + artifacts that exist.

    This is the contract the chat-first frontend renders: panels appear dynamically
    as the conversation reaches each stage (motor -> spec -> results -> export).
    """
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.workspace_for_job(job)


async def _job_event_stream(bus: Any, job_id: str, request: Request, initial: dict[str, Any]) -> AsyncIterator[dict[str, str]]:
    """Async SSE generator: current snapshot first, then live events off Redis pub/sub.

    Redis pub/sub is synchronous, so ``get_message`` runs in a threadpool to avoid
    blocking the event loop; the loop also polls ``request.is_disconnected()`` so the
    subscription is torn down when the browser closes the stream.
    """
    from anyio import to_thread

    from .events import _decode_message

    # Emit the current reflect-only state immediately so the UI renders without waiting.
    yield {"event": initial["type"], "data": json.dumps(initial, default=str)}

    pubsub = bus.subscribe(job_id)
    try:
        while True:
            if await request.is_disconnected():
                break
            message = await to_thread.run_sync(lambda: pubsub.get_message(timeout=1.0))
            event = _decode_message(message)
            if event is None:
                continue
            yield {"event": str(event.get("type", "message")), "data": json.dumps(event, default=str)}
    finally:
        try:
            await to_thread.run_sync(pubsub.close)
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, tenant: str = Depends(require_tenant)):
    """Server-Sent Events stream of live updates for one job (E2.4).

    Fans out ``message.delta`` / ``tool.started`` / ``tool.finished`` / ``run.status`` /
    ``workspace.updated`` / ``refusal`` / ``error`` events published by BOTH the API
    (chat loop) and the RQ worker (design run) over Redis pub/sub. The first event is
    always the current reflect-only workspace snapshot.
    """
    from sse_starlette.sse import EventSourceResponse

    from .events import EVENT_WORKSPACE_UPDATED, get_event_bus

    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    bus = get_event_bus()
    if bus is None:
        raise HTTPException(
            status_code=503,
            detail="Live events are disabled. Set COPILOT_EVENTS=true (needs Redis).",
        )

    initial = bus.build_event(job_id, EVENT_WORKSPACE_UPDATED, service.workspace_for_job(job))
    return EventSourceResponse(_job_event_stream(bus, job_id, request, initial), ping=15)


@app.get("/jobs/{job_id}/scorecard")
def scorecard(job_id: str, tenant: str = Depends(require_tenant)) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id, tenant_id=tenant)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if job.scorecard is None:
        raise HTTPException(status_code=404, detail="No scorecard yet.")
    # Return summary + per-scenario metrics without huge trajectory payloads by default
    scenarios = []
    for item in job.scorecard.get("scenarios", []):
        scenarios.append(
            {
                "name": item["name"],
                "metrics": item["metrics"],
                "constraints": item["constraints"],
                "scalar_score": item["scalar_score"],
            }
        )
    return {
        "controller": job.scorecard.get("controller"),
        "summary": job.scorecard.get("summary"),
        "scenarios": scenarios,
        "constraints": job.scorecard.get("constraints"),
    }


def main() -> None:
    import uvicorn

    uvicorn.run("saas.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
