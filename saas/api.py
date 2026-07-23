"""FastAPI design-job API for the local Control Design Copilot."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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
    ``COPILOT_PERSIST=true`` so the api + worker share Postgres.
    """
    from .config import get_settings

    if get_settings().persistence_enabled:
        from .repository import get_repository

        get_repository()
    yield


app = FastAPI(
    title="Control Design Copilot",
    description="Simulation-only adaptive controller design jobs (no hardware).",
    version="0.1.0",
    lifespan=_lifespan,
)


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
def create_job(body: CreateJobRequest) -> dict[str, Any]:
    try:
        job = service.create_job(plant_id=body.plant_id, mode=body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job.max_iterations = body.max_iterations
    get_job_store().save(job)
    return job.to_public_dict()


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    jobs = [j.to_public_dict() for j in get_job_store().list_jobs()]
    return {"jobs": jobs}


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/motor")
def set_motor_text(job_id: str, body: MotorTextRequest) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
        service.set_motor_from_text(job, body.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/motor/params")
def set_motor_params(job_id: str, body: MotorParamsRequest) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
        service.set_motor_from_params(job, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/interpret")
def interpret(job_id: str, body: InterpretRequest) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
        service.interpret_job_spec(job, body.text, critique=body.critique)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/clarify")
def clarify(job_id: str, body: ClarifyRequest) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
        service.answer_clarification(job, body.answer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/run")
def run_design(job_id: str, body: RunRequest | None = None) -> dict[str, Any]:
    """Start a design run.

    When async runs are enabled (Compose default) the CPU-heavy design loop is enqueued
    to the RQ worker and this returns immediately with ``status="queued"`` — poll
    ``GET /jobs/{id}`` or ``GET /jobs/{id}/status`` for completion. Otherwise it runs
    inline (host tools / tests).
    """
    from .config import get_settings

    body = body or RunRequest()
    try:
        job = get_job_store().get(job_id)
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
def run_status(job_id: str) -> dict[str, Any]:
    """Lightweight status/poll path for an (async) design run."""
    try:
        job = get_job_store().get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.run_status(job)


@app.post("/jobs/{job_id}/feedback")
def feedback(job_id: str, body: FeedbackRequest) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
        service.apply_feedback_and_maybe_rerun(
            job, body.text, use_llm=body.use_llm, rerun=body.rerun
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/agent")
def agent_chat(job_id: str, body: AgentChatRequest) -> dict[str, Any]:
    """Chat-first tool-calling Design Agent (workstream D). OpenAI-only.

    The agent owns the session and drives the deterministic engine through tools;
    every reported number comes from a tool, never from the model.
    """
    try:
        job = get_job_store().get(job_id)
        service.agent_chat(job, body.message)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return job.to_public_dict()


@app.post("/jobs/{job_id}/export")
def export(job_id: str) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
        path = service.export_job(job)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"export_path": str(path), "job": job.to_public_dict()}


@app.get("/jobs/{job_id}/export/download")
def download_export(job_id: str):
    try:
        job = get_job_store().get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not job.export_path:
        raise HTTPException(status_code=404, detail="No export yet. POST /jobs/{id}/export first.")
    path = Path(job.export_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Export file missing: {path}")
    return FileResponse(path, filename=path.name)


@app.get("/jobs/{job_id}/workspace")
def workspace(job_id: str) -> dict[str, Any]:
    """Reflect-only workspace snapshot: workflow phase + artifacts that exist.

    This is the contract the chat-first frontend renders: panels appear dynamically
    as the conversation reaches each stage (motor -> spec -> results -> export).
    """
    try:
        job = get_job_store().get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.workspace_for_job(job)


@app.get("/jobs/{job_id}/scorecard")
def scorecard(job_id: str) -> dict[str, Any]:
    try:
        job = get_job_store().get(job_id)
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
