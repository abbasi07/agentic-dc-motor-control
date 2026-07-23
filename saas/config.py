"""Runtime settings for the Control Design Copilot backend.

Values come from the environment (Docker Compose injects them via ``env_file``; for
host-run tools ``.env`` is loaded by python-dotenv). Kept dependency-free on purpose
so importing it never requires the E2 infra packages to be installed yet.

Locked invariant: secrets (OPENAI_API_KEY, DB password) live only in the gitignored
``.env`` — never in code or the image.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

# Sensible defaults target Docker Compose service names (db, redis). Host-run tools
# can override DATABASE_URL / REDIS_URL to localhost in their own .env.
_DEFAULT_DATABASE_URL = "postgresql+psycopg://copilot:copilot@db:5432/copilot"
_DEFAULT_REDIS_URL = "redis://redis:6379/0"
_DESIGN_QUEUE = "copilot"


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    design_queue: str
    openai_api_key: str | None
    openai_model: str
    # E2.5 auth: when enabled, every /jobs route requires ``Authorization: Bearer <key>``
    # (hashed API keys in Postgres) and is scoped to the key's tenant; Redis per-tenant
    # rate limits apply. Off by default so host tools / the OpenAI-free test-suite reach
    # the API unauthenticated (tenant falls back to the dev tenant). Needs persistence
    # (the api_keys table lives in the DB). Compose sets it true.
    auth_enabled: bool
    # Dev bootstrap API key seeded on startup so local/demo use needs no signup.
    dev_api_key: str | None
    # Server-side secret ("pepper") mixed into the API-key hash so the DB never stores a
    # value that reverses to the raw key. Override in production via COPILOT_API_KEY_PEPPER.
    api_key_pepper: str
    # Background guardrails (budgets); surfaced read-only in the workspace.
    max_tokens_per_session: int
    max_design_iterations: int
    rate_limit_per_minute: int
    # E2.2 persistence: when enabled, the JobStore is backed by SQLAlchemy/Postgres
    # so job + agent state survives restarts and crosses the RQ worker boundary.
    # Off by default so host-run tools / the OpenAI-free test-suite stay in-memory.
    persistence_enabled: bool
    db_echo: bool
    # E2.3 async runs: when enabled, design runs (grid + differential_evolution +
    # per-step MPC QPs — CPU-heavy/blocking) are enqueued to the RQ worker instead of
    # running inline, so FastAPI stays responsive. Off by default so host tools / the
    # OpenAI-free test-suite run synchronously; requires persistence (worker + API are
    # separate processes that share job state through the DB). Compose sets it true.
    async_runs_enabled: bool
    # E2.4 live events: when enabled, the API (chat) and the RQ worker (design run)
    # publish structured events (message.delta, tool.started/finished, run.status,
    # workspace.updated, refusal, error) over Redis pub/sub, and clients stream them
    # via the SSE endpoint. Off by default so host tools / the OpenAI-free test-suite
    # never attempt a Redis connection; Compose sets it true for the api + worker.
    events_enabled: bool

    @property
    def has_openai(self) -> bool:
        key = self.openai_api_key or ""
        return bool(key) and not key.startswith("sk-your-key")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", _DEFAULT_DATABASE_URL),
        redis_url=os.getenv("REDIS_URL", _DEFAULT_REDIS_URL),
        design_queue=os.getenv("DESIGN_QUEUE", _DESIGN_QUEUE),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-nano"),
        auth_enabled=_bool_env("COPILOT_AUTH", False),
        dev_api_key=os.getenv("COPILOT_DEV_API_KEY"),
        api_key_pepper=os.getenv("COPILOT_API_KEY_PEPPER", "copilot-dev-pepper"),
        max_tokens_per_session=_int_env("MAX_TOKENS_PER_SESSION", 200_000),
        max_design_iterations=_int_env("MAX_DESIGN_ITERATIONS", 12),
        rate_limit_per_minute=_int_env("RATE_LIMIT_PER_MINUTE", 60),
        persistence_enabled=_bool_env("COPILOT_PERSIST", False),
        db_echo=_bool_env("COPILOT_DB_ECHO", False),
        async_runs_enabled=_bool_env("COPILOT_ASYNC_RUNS", False),
        events_enabled=_bool_env("COPILOT_EVENTS", False),
    )


__all__ = ["Settings", "get_settings"]
