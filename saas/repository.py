"""DB-backed job repository (E2.2 persistence).

Drop-in replacement for the in-memory :class:`saas.jobs.JobStore` that serializes each
:class:`saas.jobs.DesignJob` to Postgres so state survives an API restart *and* crosses
the RQ worker process boundary. It keeps an in-process cache of live jobs (so live
controller / agent objects are reused within a single process) while using a monotonic
``rev`` column to detect when another process (the worker) has updated a job and the
cache must rehydrate from the DB.

Rehydration reuses the existing JSON round-trip: ``DesignJob.to_record`` /
``DesignJob.from_record`` (which lean on ``design_spec_from_dict`` /
``motor_model_from_dict`` lazily via the service layer). The live controller is not
serialized; the export/certify gate is fed a stub from ``saas.serialization``.
"""

from __future__ import annotations

import threading
import uuid
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dc_motor.registry import DEFAULT_PLANT_ID

from .db import get_session_factory, init_db
from .jobs import DesignJob
from .models import (
    AgentSessionRow,
    ArtifactRow,
    DesignJobRow,
    MessageRow,
    Tenant,
    ToolCallRow,
)
from .serialization import to_jsonable

DEFAULT_TENANT_ID = "dev"
DEFAULT_TENANT_NAME = "Local Dev Tenant"

# Reflect-only artifact kinds mirrored into the `artifacts` table (bounded JSON only —
# the full scorecard with trajectories stays in design_jobs.data).
_ARTIFACT_SOURCES: tuple[str, ...] = (
    "motor",
    "spec",
    "feasibility",
    "certification",
    "export",
)


class JobRepository:
    """Persistent job store backed by SQLAlchemy (same surface as ``JobStore``)."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._sf = session_factory or get_session_factory()
        self._cache: dict[str, DesignJob] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Bootstrap
    # ------------------------------------------------------------------ #
    def ensure_default_tenant(self) -> str:
        return self.ensure_tenant(DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME)

    def ensure_tenant(self, tenant_id: str, name: str | None = None) -> str:
        """Idempotently create a tenant row (keeps the design_jobs FK satisfiable)."""
        with self._sf() as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None:
                default_name = (
                    DEFAULT_TENANT_NAME if tenant_id == DEFAULT_TENANT_ID else tenant_id
                )
                session.add(Tenant(id=tenant_id, name=name or default_name))
                session.commit()
        return tenant_id

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    def create(
        self,
        *,
        plant_id: str = DEFAULT_PLANT_ID,
        mode: str = "heuristic",
        tenant_id: str | None = None,
    ) -> DesignJob:
        # Guarantee the tenant row exists so the design_jobs FK holds (the auth path
        # already creates real tenants; this covers the dev tenant + direct callers).
        tenant_id = self.ensure_tenant(tenant_id) if tenant_id else self.ensure_default_tenant()
        job = DesignJob(
            job_id=str(uuid.uuid4()), plant_id=plant_id, mode=mode, tenant_id=tenant_id
        )
        self.save(job)
        return job

    def get(self, job_id: str, tenant_id: str | None = None) -> DesignJob:
        with self._lock:
            with self._sf() as session:
                row = session.get(DesignJobRow, job_id)
                if row is None:
                    raise KeyError(f"Unknown job_id={job_id}")
                # Tenant scoping (E2.5): raise KeyError (surfaced as 404) rather than a
                # 403 so job existence never leaks across tenants.
                if (
                    tenant_id is not None
                    and row.tenant_id is not None
                    and row.tenant_id != tenant_id
                ):
                    raise KeyError(f"Unknown job_id={job_id}")
                cached = self._cache.get(job_id)
                # Reuse the live object (keeps _session/_agent) only if it is not stale.
                if cached is not None and cached._rev >= row.rev:
                    return cached
                job = DesignJob.from_record(dict(row.data or {}))
                job._rev = row.rev
                self._cache[job_id] = job
                return job

    def save(self, job: DesignJob) -> DesignJob:
        # Coerce numpy / NaN-Inf out of the scorecard + tool logs so the JSON columns
        # round-trip on both SQLite and Postgres JSONB.
        record = to_jsonable(job.to_record())
        with self._lock:
            with self._sf() as session:
                row = session.get(DesignJobRow, job.job_id)
                new_rev = (row.rev + 1) if row is not None else 1
                if row is None:
                    row = DesignJobRow(job_id=job.job_id)
                    session.add(row)
                row.tenant_id = job.tenant_id
                row.plant_id = job.plant_id or ""
                row.status = job.status
                row.mode = job.mode
                row.rev = new_rev
                row.data = record
                self._write_agent_session(session, job, record)
                self._write_projections(session, job, record)
                session.commit()
            job._rev = new_rev
            self._cache[job.job_id] = job
        return job

    def list_jobs(self, tenant_id: str | None = None) -> list[DesignJob]:
        with self._lock:
            with self._sf() as session:
                stmt = select(DesignJobRow)
                if tenant_id is not None:
                    stmt = stmt.where(DesignJobRow.tenant_id == tenant_id)
                rows = session.execute(
                    stmt.order_by(DesignJobRow.created_at)
                ).scalars().all()
                jobs: list[DesignJob] = []
                for row in rows:
                    cached = self._cache.get(row.job_id)
                    if cached is not None and cached._rev >= row.rev:
                        jobs.append(cached)
                        continue
                    job = DesignJob.from_record(dict(row.data or {}))
                    job._rev = row.rev
                    self._cache[row.job_id] = job
                    jobs.append(job)
                return jobs

    def delete(self, job_id: str, tenant_id: str | None = None) -> None:
        """Permanently remove a job and its cascaded projections / agent session."""
        with self._lock:
            with self._sf() as session:
                row = session.get(DesignJobRow, job_id)
                if row is None:
                    raise KeyError(f"Unknown job_id={job_id}")
                if (
                    tenant_id is not None
                    and row.tenant_id is not None
                    and row.tenant_id != tenant_id
                ):
                    raise KeyError(f"Unknown job_id={job_id}")
                session.delete(row)
                session.commit()
            self._cache.pop(job_id, None)

    # ------------------------------------------------------------------ #
    # Normalized projections (write-through; design_jobs.data is source of truth)
    # ------------------------------------------------------------------ #
    def _write_agent_session(
        self, session: Session, job: DesignJob, record: dict[str, Any]
    ) -> None:
        agent_state = record.get("agent_state")
        srow = session.get(AgentSessionRow, job.job_id)
        if not agent_state:
            if srow is not None:
                session.delete(srow)
            return
        if srow is None:
            srow = AgentSessionRow(job_id=job.job_id)
            session.add(srow)
        srow.data = agent_state
        srow.total_tokens = int(agent_state.get("total_tokens", 0) or 0)

    def _write_projections(
        self, session: Session, job: DesignJob, record: dict[str, Any]
    ) -> None:
        # Rewrite chat + tool_log + artifact projections for this job.
        for model in (MessageRow, ToolCallRow, ArtifactRow):
            for obj in session.execute(
                select(model).where(model.job_id == job.job_id)
            ).scalars().all():
                session.delete(obj)
        session.flush()

        for seq, msg in enumerate(record.get("chat") or []):
            session.add(
                MessageRow(
                    job_id=job.job_id,
                    seq=seq,
                    role=str(msg.get("role", "assistant")),
                    content=str(msg.get("content", "")),
                )
            )

        tool_log = (record.get("agent_state") or {}).get("tool_log") or []
        for seq, call in enumerate(tool_log):
            session.add(
                ToolCallRow(
                    job_id=job.job_id,
                    seq=seq,
                    tool=str(call.get("tool", "")),
                    args=call.get("args") or {},
                    result=call.get("result") or {},
                )
            )

        for kind in _ARTIFACT_SOURCES:
            payload = _artifact_payload(kind, record)
            if payload:
                session.add(ArtifactRow(job_id=job.job_id, kind=kind, payload=payload))


def _artifact_payload(kind: str, record: dict[str, Any]) -> dict[str, Any] | None:
    if kind == "motor":
        return record.get("motor_dict")
    if kind == "spec":
        return record.get("spec_dict")
    if kind == "feasibility":
        return record.get("feasibility")
    if kind == "certification":
        return record.get("certification")
    if kind == "export":
        path = record.get("export_path")
        return {"path": path, "status": record.get("status")} if path else None
    return None


_REPO: JobRepository | None = None


@lru_cache(maxsize=1)
def _bootstrap_once() -> bool:
    """Ensure tables exist + a default tenant is present (idempotent)."""
    init_db()
    return True


def get_repository() -> JobRepository:
    global _REPO
    if _REPO is None:
        _bootstrap_once()
        _REPO = JobRepository()
        _REPO.ensure_default_tenant()
    return _REPO


__all__ = ["DEFAULT_TENANT_ID", "JobRepository", "get_repository"]
