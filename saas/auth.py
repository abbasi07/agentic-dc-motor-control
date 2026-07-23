"""API-key authentication + multi-tenant scoping (E2.5).

Auth v1 is intentionally simple and self-serve: a tenant holds one or more API keys;
requests authenticate with ``Authorization: Bearer <key>``; a dev bootstrap key
(``COPILOT_DEV_API_KEY``) is seeded on startup so local/demo use needs no signup.

Locked invariant: the plaintext key is shown exactly once (at creation) and NEVER
stored. The DB keeps only a salted/peppered SHA-256 hash. Verification is a single
indexed lookup on that hash, so the hash must be deterministic given the key — we mix
in a server-side ``pepper`` (``COPILOT_API_KEY_PEPPER``) rather than a per-row random
salt (which would force a full-table scan on every request).

Everything here is OpenAI-free and testable over SQLite: construct an
:class:`AuthManager` with an explicit ``session_factory`` (see ``tests/test_auth.py``).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import get_session_factory
from .models import ApiKey, Tenant
from .repository import DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME

_KEY_PREFIX = "cdc_"


class BudgetExceeded(RuntimeError):
    """Raised when a per-session token / iteration budget is exhausted (maps to HTTP 429)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_api_key() -> str:
    """Return a fresh random API key (shown to the user once, never stored raw)."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


class AuthManager:
    """Create / verify hashed API keys and scope them to tenants.

    Stateless apart from the DB; a single process-wide instance is fine (see
    :func:`get_auth_manager`). The ``pepper`` defaults to the configured server secret
    but can be injected for tests.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        pepper: str | None = None,
    ) -> None:
        self._sf = session_factory or get_session_factory()
        if pepper is None:
            from .config import get_settings

            pepper = get_settings().api_key_pepper
        self._pepper = pepper

    # ------------------------------------------------------------------ #
    # Hashing
    # ------------------------------------------------------------------ #
    def hash_key(self, raw: str) -> str:
        """Deterministic peppered SHA-256 hex digest (64 chars) of a raw key."""
        return hashlib.sha256(f"{self._pepper}:{raw}".encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Tenants
    # ------------------------------------------------------------------ #
    def ensure_tenant(self, tenant_id: str, name: str | None = None) -> str:
        with self._sf() as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None:
                session.add(Tenant(id=tenant_id, name=name or tenant_id))
                session.commit()
        return tenant_id

    # ------------------------------------------------------------------ #
    # API keys
    # ------------------------------------------------------------------ #
    def create_api_key(
        self, tenant_id: str, *, label: str = "", raw: str | None = None
    ) -> str:
        """Create an API key for ``tenant_id`` and return the RAW key (shown once).

        Only the hash is persisted. Pass ``raw`` to register a known key (used by the
        dev bootstrap); otherwise a random one is generated.
        """
        raw = raw or generate_api_key()
        with self._sf() as session:
            session.add(
                ApiKey(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    key_hash=self.hash_key(raw),
                    label=label,
                    active=True,
                )
            )
            session.commit()
        return raw

    def verify_api_key(self, raw: str | None) -> str | None:
        """Return the tenant id for a valid active key, or ``None``.

        Touches ``last_used_at`` on success. A single indexed lookup on the hash.
        """
        if not raw:
            return None
        key_hash = self.hash_key(raw)
        with self._sf() as session:
            row = session.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.active.is_(True))
            ).scalar_one_or_none()
            if row is None:
                return None
            row.last_used_at = _utcnow()
            tenant_id = row.tenant_id
            session.commit()
            return tenant_id

    def seed_dev_api_key(self) -> str | None:
        """Idempotently register the configured dev bootstrap key on the dev tenant.

        No-op when ``COPILOT_DEV_API_KEY`` is unset. Returns the dev tenant id when a
        key exists/was created, else ``None``.
        """
        from .config import get_settings

        raw = get_settings().dev_api_key
        if not raw:
            return None
        self.ensure_tenant(DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME)
        key_hash = self.hash_key(raw)
        with self._sf() as session:
            existing = session.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ApiKey(
                        id=str(uuid.uuid4()),
                        tenant_id=DEFAULT_TENANT_ID,
                        key_hash=key_hash,
                        label="dev bootstrap",
                        active=True,
                    )
                )
                session.commit()
        return DEFAULT_TENANT_ID


_AUTH: AuthManager | None = None


def get_auth_manager() -> AuthManager:
    """Process-wide :class:`AuthManager`, ensuring the schema exists first."""
    global _AUTH
    if _AUTH is None:
        from .repository import _bootstrap_once

        _bootstrap_once()
        _AUTH = AuthManager()
    return _AUTH


__all__ = [
    "AuthManager",
    "BudgetExceeded",
    "generate_api_key",
    "get_auth_manager",
]
