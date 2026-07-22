"""In-memory design job store for the local Design Copilot API."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dc_motor.motor_model import MotorModel
from dc_motor.registry import DEFAULT_PLANT_ID
from dc_motor.specs import DesignSpec

JobStatus = Literal[
    "draft",
    "needs_clarification",
    "spec_ready",
    "running",
    "completed",
    "failed",
    "exported",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class DesignJob:
    job_id: str
    plant_id: str = DEFAULT_PLANT_ID
    status: JobStatus = "draft"
    nl_spec: str = ""
    mode: str = "heuristic"
    max_iterations: int = 5
    chat: list[dict[str, str]] = field(default_factory=list)
    clarifying_questions: list[str] = field(default_factory=list)
    spec_dict: dict[str, Any] | None = None
    # Custom (chat-defined) DC motor. When set, it overrides the registry plant.
    motor_dict: dict[str, Any] | None = None
    feasibility: dict[str, Any] | None = None
    confirmed: bool = False
    session_dict: dict[str, Any] | None = None
    scorecard: dict[str, Any] | None = None
    certification: dict[str, Any] | None = None
    export_path: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    # Keep live objects for export / feedback (not always JSON-serializable)
    _spec: DesignSpec | None = field(default=None, repr=False)
    _session: Any = field(default=None, repr=False)
    _motor: MotorModel | None = field(default=None, repr=False)
    # Chat-first tool-calling Design Agent session (workstream D), lazily created.
    _agent: Any = field(default=None, repr=False)

    def touch(self) -> None:
        self.updated_at = _utc_now()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "plant_id": self.plant_id,
            "status": self.status,
            "nl_spec": self.nl_spec,
            "mode": self.mode,
            "max_iterations": self.max_iterations,
            "chat": list(self.chat),
            "clarifying_questions": list(self.clarifying_questions),
            "spec": self.spec_dict,
            "motor": self.motor_dict,
            "feasibility": self.feasibility,
            "confirmed": self.confirmed,
            "session": self.session_dict,
            "scorecard_summary": None
            if self.scorecard is None
            else self.scorecard.get("summary"),
            "certification": self.certification,
            "export_path": self.export_path,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, DesignJob] = {}
        self._lock = threading.Lock()

    def create(self, *, plant_id: str = DEFAULT_PLANT_ID, mode: str = "heuristic") -> DesignJob:
        job = DesignJob(job_id=str(uuid.uuid4()), plant_id=plant_id, mode=mode)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> DesignJob:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"Unknown job_id={job_id}")
            return self._jobs[job_id]

    def list_jobs(self) -> list[DesignJob]:
        with self._lock:
            return list(self._jobs.values())


_STORE: JobStore | None = None


def get_job_store() -> JobStore:
    global _STORE
    if _STORE is None:
        _STORE = JobStore()
    return _STORE


def default_export_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "exports"
