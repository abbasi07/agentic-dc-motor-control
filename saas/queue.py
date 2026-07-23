"""RQ queue + worker task for async design runs (E2.3).

Design runs are CPU-heavy/blocking (grid search + ``differential_evolution`` +
per-step MPC QPs), so instead of running them inline in the FastAPI request we enqueue
them to an RQ worker (queue name = ``settings.design_queue``, default ``copilot``). The
API returns immediately with ``status="queued"``; the worker flips the job to
``running`` -> ``completed``/``failed`` and persists the result. Because the worker and
API are **separate processes with separate in-process caches**, the result reaches the
API through the E2.2 serialize/rehydrate + ``rev`` contract (the worker writes, the API
rehydrates from the DB on the next poll).

The task function :func:`run_design_job` is module-level (RQ references it by import
path) and rehydrates the job from the active store before running it — it never receives
a live controller across the process boundary.

Testability: pass a queue built with ``is_async=False`` (over ``fakeredis``) to run the
job inline with no worker/real Redis, or drive an ``rq.SimpleWorker``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis import Redis
    from rq import Queue
    from rq.job import Job


@lru_cache(maxsize=1)
def get_redis_connection() -> "Redis":
    """Process-wide Redis connection built from ``settings.redis_url``."""
    import redis

    return redis.Redis.from_url(get_settings().redis_url)


def get_queue(connection: "Redis | None" = None, *, is_async: bool = True) -> "Queue":
    """Return the design-run RQ queue (``settings.design_queue``)."""
    from rq import Queue

    conn = connection or get_redis_connection()
    return Queue(get_settings().design_queue, connection=conn, is_async=is_async)


# --------------------------------------------------------------------------- #
# Worker task (runs in the RQ worker process)
# --------------------------------------------------------------------------- #
def run_design_job(
    job_id: str,
    *,
    max_iterations: int | None = None,
    maxiter_scipy: int = 8,
) -> dict[str, Any]:
    """Execute a queued design run inside the worker.

    Rehydrates the job from the active (DB-backed) store, runs the deterministic design
    loop via :func:`saas.service.confirm_and_run`, and lets that persist the result so
    the API can serve it via the rehydrate path. Returns a small JSON-safe summary
    (stored by RQ as the job result); the authoritative state is the persisted job.
    """
    # Imported lazily so importing this module never pulls in the heavy service layer.
    from . import service
    from .jobs import get_job_store

    job = get_job_store().get(job_id)
    service.confirm_and_run(job, max_iterations=max_iterations, maxiter_scipy=maxiter_scipy)
    return {"job_id": job_id, "status": job.status, "error": job.error}


# --------------------------------------------------------------------------- #
# Enqueue (called from the API/service process)
# --------------------------------------------------------------------------- #
def enqueue_design_run(
    job_id: str,
    *,
    max_iterations: int | None = None,
    maxiter_scipy: int = 8,
    queue: "Queue | None" = None,
    job_timeout: int = 900,
) -> "Job":
    """Enqueue a design run for ``job_id`` and return the RQ job handle."""
    q = queue or get_queue()
    return q.enqueue(
        run_design_job,
        job_id,
        max_iterations=max_iterations,
        maxiter_scipy=maxiter_scipy,
        job_id=f"design-{job_id}",
        job_timeout=job_timeout,
    )


def fetch_queue_job(queue_job_id: str, queue: "Queue | None" = None) -> "Job | None":
    """Fetch an RQ job by id (for the status/poll path); ``None`` if unknown/expired."""
    from rq.job import Job

    q = queue or get_queue()
    try:
        return Job.fetch(queue_job_id, connection=q.connection)
    except Exception:  # noqa: BLE001 - poll must never raise (unknown/expired id)
        return None


__all__ = [
    "enqueue_design_run",
    "fetch_queue_job",
    "get_queue",
    "get_redis_connection",
    "run_design_job",
]
