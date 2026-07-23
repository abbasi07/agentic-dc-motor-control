"""SQLAlchemy ORM models for the Control Design Copilot (E2 persistence).

Schema overview (multi-tenant SaaS from the start):

    tenants        — an org/account; every design job belongs to one tenant.
    api_keys       — hashed API keys per tenant (Bearer auth, E2.5). Never store raw keys.
    design_jobs    — the authoritative, rehydratable job state. Bulk JSON state lives in
                     ``data`` (round-trips via saas.serialization); a few columns are
                     promoted for querying/scoping (tenant_id, status, plant_id, rev).
    agent_sessions — chat-first Design Agent transcript state (messages/tool_log/tokens),
                     one row per job, so the tool-calling loop survives a restart.
    messages       — normalized projection of the user-visible chat (audit/history pane).
    tool_calls     — normalized projection of the agent tool_log (audit / ablation).
    artifacts      — normalized projection of the reflect-only workspace artifacts.

``design_jobs.data`` + ``agent_sessions.data`` are the source of truth for rehydration.
The ``messages`` / ``tool_calls`` / ``artifacts`` tables are write-through projections
(rewritten on each save) that make the history queryable without deserializing JSON.

Locked invariant: raw secrets never touch the DB — api_keys stores only a salted hash.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Salted hash only — the plaintext key is shown once at creation and never stored.
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


class DesignJobRow(Base):
    __tablename__ = "design_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=True
    )
    plant_id: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft")
    mode: Mapped[str] = mapped_column(String(32), default="heuristic")
    # Monotonic revision: bumped on every save so a stale in-process cache (e.g. the API
    # holding a job the worker just updated) can detect it must rehydrate from the DB.
    rev: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Full rehydratable job state (spec_dict, motor_dict, scorecard, chat, …).
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    agent_session: Mapped["AgentSessionRow | None"] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    messages: Mapped[list["MessageRow"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    tool_calls: Mapped[list["ToolCallRow"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list["ArtifactRow"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class AgentSessionRow(Base):
    __tablename__ = "agent_sessions"

    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("design_jobs.job_id", ondelete="CASCADE"), primary_key=True
    )
    # {"messages": [...], "tool_log": [...], "total_tokens": int, "model": str|None}
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    job: Mapped[DesignJobRow] = relationship(back_populates="agent_session")


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("design_jobs.job_id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(32), default="assistant")
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[DesignJobRow] = relationship(back_populates="messages")


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("design_jobs.job_id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0)
    tool: Mapped[str] = mapped_column(String(64), default="")
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[DesignJobRow] = relationship(back_populates="tool_calls")


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("job_id", "kind", name="uq_artifacts_job_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("design_jobs.job_id", ondelete="CASCADE"), index=True
    )
    # motor | spec | feasibility | results | plots | certification | export
    kind: Mapped[str] = mapped_column(String(32), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    job: Mapped[DesignJobRow] = relationship(back_populates="artifacts")


__all__ = [
    "AgentSessionRow",
    "ApiKey",
    "ArtifactRow",
    "DesignJobRow",
    "MessageRow",
    "Tenant",
    "ToolCallRow",
]
