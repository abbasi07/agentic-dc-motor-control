"""In-memory design job store for the local Design Copilot API."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Literal

from dc_motor.motor_model import MotorModel
from dc_motor.registry import DEFAULT_PLANT_ID
from dc_motor.specs import DesignSpec

JobStatus = Literal[
    "draft",
    "needs_clarification",
    "spec_ready",
    "queued",
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
    # Chat-first negotiation gates: the engineer must explicitly agree to the motor
    # and the spec before the workflow advances (see agents/workflow.py phases).
    motor_confirmed: bool = False
    spec_confirmed: bool = False
    session_dict: dict[str, Any] | None = None
    scorecard: dict[str, Any] | None = None
    certification: dict[str, Any] | None = None
    export_path: str | None = None
    error: str | None = None
    # E2.3 async runs: the RQ job id of the enqueued design run (if any), so the
    # status/poll path can surface queue state. Lives in the JSON `data` column.
    queue_job_id: str | None = None
    # Multi-tenant scoping (auth wires this in E2.5; nullable for local/dev use).
    tenant_id: str | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    # Persisted Design Agent snapshot ({messages, tool_log, total_tokens, model}) so
    # the tool-calling loop can be rehydrated after a restart / in the worker process.
    agent_state: dict[str, Any] | None = field(default=None, repr=False)
    # Keep live objects for export / feedback (not always JSON-serializable). These are
    # rebuilt lazily from the *_dict fields after a rehydrate (see saas.service).
    _spec: DesignSpec | None = field(default=None, repr=False)
    _session: Any = field(default=None, repr=False)
    _motor: MotorModel | None = field(default=None, repr=False)
    # Chat-first tool-calling Design Agent session (workstream D), lazily created.
    _agent: Any = field(default=None, repr=False)
    # Cache revision mirrored from design_jobs.rev; lets a store notice a job was
    # updated by another process (worker) and rehydrate instead of serving stale state.
    _rev: int = field(default=0, repr=False)

    def touch(self) -> None:
        self.updated_at = _utc_now()

    # ------------------------------------------------------------------ #
    # Persistence round-trip (JSON record <-> live job). Live objects are
    # intentionally excluded: _spec/_motor rebuild from *_dict, and the design
    # result is rehydrated from session_dict/scorecard (see saas.serialization).
    # ------------------------------------------------------------------ #
    _RECORD_FIELDS: ClassVar[tuple[str, ...]] = (
        "job_id",
        "plant_id",
        "status",
        "nl_spec",
        "mode",
        "max_iterations",
        "chat",
        "clarifying_questions",
        "spec_dict",
        "motor_dict",
        "feasibility",
        "confirmed",
        "motor_confirmed",
        "spec_confirmed",
        "session_dict",
        "scorecard",
        "certification",
        "export_path",
        "error",
        "queue_job_id",
        "tenant_id",
        "created_at",
        "updated_at",
        "agent_state",
    )

    def to_record(self) -> dict[str, Any]:
        """JSON-serializable snapshot of all durable state (no live objects)."""
        record = {name: getattr(self, name) for name in self._RECORD_FIELDS}
        # Fold the live agent transcript into the record if it is attached.
        if self._agent is not None and hasattr(self._agent, "snapshot"):
            record["agent_state"] = self._agent.snapshot()
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "DesignJob":
        """Rebuild a DesignJob from a persisted record (live objects stay lazy)."""
        known = {k: v for k, v in record.items() if k in cls._RECORD_FIELDS}
        return cls(**known)

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
            "motor_confirmed": self.motor_confirmed,
            "spec_confirmed": self.spec_confirmed,
            "session": self.session_dict,
            "scorecard_summary": None
            if self.scorecard is None
            else self.scorecard.get("summary"),
            "certification": self.certification,
            "export_path": self.export_path,
            "error": self.error,
            "queue_job_id": self.queue_job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobStore:
    """In-memory job store (default for host tools + the OpenAI-free test-suite).

    The DB-backed :class:`saas.repository.JobRepository` implements the same
    ``create``/``get``/``save``/``list_jobs`` surface, so the service layer is agnostic
    to whether persistence is enabled.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, DesignJob] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        plant_id: str = DEFAULT_PLANT_ID,
        mode: str = "heuristic",
        tenant_id: str | None = None,
    ) -> DesignJob:
        job = DesignJob(
            job_id=str(uuid.uuid4()), plant_id=plant_id, mode=mode, tenant_id=tenant_id
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str, tenant_id: str | None = None) -> DesignJob:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"Unknown job_id={job_id}")
            job = self._jobs[job_id]
        # Tenant scoping (E2.5): a caller scoped to a tenant may only see its own jobs.
        # Raise KeyError (not a 403) so job existence is not leaked across tenants.
        if tenant_id is not None and job.tenant_id is not None and job.tenant_id != tenant_id:
            raise KeyError(f"Unknown job_id={job_id}")
        return job

    def save(self, job: DesignJob) -> DesignJob:
        """No-op for the in-memory store (the live object is already retained)."""
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def list_jobs(self, tenant_id: str | None = None) -> list[DesignJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        if tenant_id is not None:
            jobs = [j for j in jobs if j.tenant_id == tenant_id]
        return jobs

    def delete(self, job_id: str, tenant_id: str | None = None) -> None:
        """Remove a job. Raises KeyError if missing or tenant-scoped out."""
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"Unknown job_id={job_id}")
            job = self._jobs[job_id]
            if (
                tenant_id is not None
                and job.tenant_id is not None
                and job.tenant_id != tenant_id
            ):
                raise KeyError(f"Unknown job_id={job_id}")
            del self._jobs[job_id]


_STORE: Any = None


def _build_default_store() -> Any:
    """In-memory by default; DB-backed repository when persistence is enabled."""
    from .config import get_settings

    if get_settings().persistence_enabled:
        from .repository import get_repository

        return get_repository()
    return JobStore()


def get_job_store() -> Any:
    global _STORE
    if _STORE is None:
        _STORE = _build_default_store()
    return _STORE


def default_export_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "exports"
