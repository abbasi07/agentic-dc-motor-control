"""E2.3 async design-run tests: RQ enqueue + worker task (no real Redis, no OpenAI).

The design run itself is patched (``run_design_session``) — these tests verify the
*plumbing*: the job is marked ``queued`` and enqueued, the worker task rehydrates the
job from the (SQLite) store and flips it to ``completed``/``failed``, the result reaches
a "fresh process" via the E2.2 ``rev`` rehydrate path, and the API ``/run`` +
``/status`` routes behave. RQ runs over ``fakeredis`` (inline ``is_async=False`` queue
or ``SimpleWorker`` burst); state persists over a shared on-disk SQLite DB.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import fakeredis
import pytest
from rq import Queue, SimpleWorker
from sqlalchemy.orm import sessionmaker

from agents.design_candidate import candidate_from_tune_result
from agents.orchestrator import DesignSession
from agents.pid_tuner import tune_pid
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec
from saas.db import init_db, make_engine
from saas.repository import JobRepository


@pytest.fixture()
def db(tmp_path):
    """File-backed SQLite session factory shared across repositories (= processes)."""
    url = f"sqlite:///{tmp_path / 'copilot_async.db'}"
    engine = make_engine(url)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def redis_conn():
    return fakeredis.FakeStrictRedis()


def _easy_spec() -> DesignSpec:
    return validate_and_clamp_design_spec(
        DesignSpec(
            raw_spec="easy",
            hard_constraints={
                "settling_time_s": ("<=", 2.0),
                "overshoot_pct": ("<=", 15.0),
                "steady_state_error": ("<=", 0.05),
            },
            required_scenarios=["step_1rads"],
            source="manual",
        )
    )


def _fake_session(spec: DesignSpec) -> DesignSession:
    """A realistic passed session (real scorecard) with no orchestrator run."""
    real = tune_pid(spec, method="grid", grid={"Kp": [100.0], "Ki": [200.0], "Kd": [10.0]})
    cand = candidate_from_tune_result(real)
    return DesignSession(
        nl_spec="easy", mode="script", spec=spec, status="passed", best=cand,
        rationale="ok", total_wall_time_s=0.1, total_tool_evaluations=1, total_tokens=0,
    )


def _spec_ready_job(repo: JobRepository):
    """Create a job that is ready to run (spec present, spec_ready)."""
    spec = _easy_spec()
    job = repo.create(plant_id="dc_motor_ctms", mode="script")
    job.nl_spec = "easy"
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.status = "spec_ready"
    repo.save(job)
    return job, spec


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_async_runs_disabled_by_default():
    from saas.config import Settings, get_settings

    # Default construction (no COPILOT_ASYNC_RUNS in the test env).
    assert isinstance(get_settings(), Settings)
    assert get_settings().async_runs_enabled is False


# --------------------------------------------------------------------------- #
# service.enqueue_design_run
# --------------------------------------------------------------------------- #
def test_enqueue_marks_queued_and_records_queue_job_id(db, redis_conn, monkeypatch):
    """With a real (async) queue but no worker, the job stays queued + is recorded."""
    import saas.jobs as jobs_mod
    from saas import service

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)
    job, _ = _spec_ready_job(repo)

    q = Queue("copilot", connection=redis_conn, is_async=True)
    service.enqueue_design_run(job, queue=q)

    assert job.status == "queued"
    assert job.queue_job_id == f"design-{job.job_id}"

    # Persisted as queued so a poll from another process reflects it immediately.
    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got.status == "queued"
    assert got.queue_job_id == f"design-{job.job_id}"
    # The RQ job really is on the (fake) queue.
    assert q.get_job_ids() == [f"design-{job.job_id}"]


def test_enqueue_requires_a_spec(db, redis_conn, monkeypatch):
    import saas.jobs as jobs_mod
    from saas import service

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)
    job = repo.create(plant_id="dc_motor_ctms")  # no spec yet

    q = Queue("copilot", connection=redis_conn, is_async=True)
    with pytest.raises(RuntimeError):
        service.enqueue_design_run(job, queue=q)


def test_inline_queue_runs_design_and_persists(db, redis_conn, monkeypatch):
    """is_async=False runs the task inline: queued -> completed, all persisted."""
    import saas.jobs as jobs_mod
    from saas import service

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)
    job, spec = _spec_ready_job(repo)

    inline_q = Queue("copilot", connection=redis_conn, is_async=False)
    with patch("saas.service.run_design_session", return_value=_fake_session(spec)):
        service.enqueue_design_run(job, max_iterations=1, queue=inline_q)

    assert job.status == "completed"
    assert job.max_iterations == 1
    assert job.queue_job_id == f"design-{job.job_id}"

    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got.status == "completed"
    assert got.certification is not None
    assert got.scorecard["summary"]["all_constraints_pass"] in (True, False)


# --------------------------------------------------------------------------- #
# Worker task across the process boundary
# --------------------------------------------------------------------------- #
def test_worker_task_runs_in_fresh_store_and_api_rehydrates(db, monkeypatch):
    """Simulate api (enqueue) + worker (run) as two processes over one DB."""
    import saas.jobs as jobs_mod
    from saas import queue as queue_mod

    # --- API side: mark queued (no live design object) ---
    api_repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", api_repo)
    job, spec = _spec_ready_job(api_repo)
    job.status = "queued"
    api_repo.save(job)

    # --- Worker side: fresh store (empty cache = fresh process) runs the task ---
    worker_repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", worker_repo)
    with patch("saas.service.run_design_session", return_value=_fake_session(spec)):
        result = queue_mod.run_design_job(job.job_id, max_iterations=1)

    assert result["status"] == "completed"

    # --- API side rehydrates the worker's result via the rev bump ---
    monkeypatch.setattr(jobs_mod, "_STORE", api_repo)
    refreshed = api_repo.get(job.job_id)
    assert refreshed.status == "completed"
    assert refreshed.scorecard is not None


def test_simple_worker_drains_queue(db, redis_conn, monkeypatch):
    """End-to-end: enqueue on an async queue, drain with an RQ SimpleWorker (burst)."""
    import saas.jobs as jobs_mod
    from saas import service

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)
    job, spec = _spec_ready_job(repo)

    q = Queue("copilot", connection=redis_conn, is_async=True)
    with patch("saas.service.run_design_session", return_value=_fake_session(spec)):
        service.enqueue_design_run(job, max_iterations=1, queue=q)
        assert job.status == "queued"

        worker = SimpleWorker([q], connection=redis_conn)
        worker.work(burst=True)

    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got.status == "completed"
    assert got.scorecard is not None


# --------------------------------------------------------------------------- #
# API routes
# --------------------------------------------------------------------------- #
def _async_settings(monkeypatch, enabled: bool) -> None:
    from saas import config

    base = config.get_settings()
    monkeypatch.setattr(
        config, "get_settings", lambda: dataclasses.replace(base, async_runs_enabled=enabled)
    )


def test_run_route_enqueues_when_async_enabled(db, redis_conn, monkeypatch):
    from fastapi.testclient import TestClient

    import saas.jobs as jobs_mod
    from saas import queue as queue_mod
    from saas.api import app

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)
    _async_settings(monkeypatch, True)

    inline_q = Queue("copilot", connection=redis_conn, is_async=False)
    monkeypatch.setattr(queue_mod, "get_queue", lambda *a, **k: inline_q)

    client = TestClient(app)
    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms", "mode": "script"}).json()[
        "job_id"
    ]
    # Attach a spec directly (skip the OpenAI interpret path).
    spec = _easy_spec()
    job = repo.get(job_id)
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.nl_spec = "easy"
    job.status = "spec_ready"
    repo.save(job)

    with patch("saas.service.run_design_session", return_value=_fake_session(spec)):
        out = client.post(f"/jobs/{job_id}/run", json={"max_iterations": 1}).json()

    # Inline queue ran the task synchronously, so by the time /run returns it is done.
    assert out["status"] == "completed"
    assert out["queue_job_id"] == f"design-{job_id}"

    status = client.get(f"/jobs/{job_id}/status").json()
    assert status["job_id"] == job_id
    assert status["status"] == "completed"
    assert status["queue_job_id"] == f"design-{job_id}"


def test_run_route_runs_inline_when_async_disabled(db, monkeypatch):
    from fastapi.testclient import TestClient

    import saas.jobs as jobs_mod
    from saas.api import app

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)
    _async_settings(monkeypatch, False)

    client = TestClient(app)
    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms", "mode": "script"}).json()[
        "job_id"
    ]
    spec = _easy_spec()
    job = repo.get(job_id)
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.nl_spec = "easy"
    job.status = "spec_ready"
    repo.save(job)

    with patch("saas.service.run_design_session", return_value=_fake_session(spec)):
        out = client.post(f"/jobs/{job_id}/run", json={"max_iterations": 1}).json()

    assert out["status"] == "completed"
    # No queue involved on the synchronous path.
    assert out["queue_job_id"] is None


def test_status_route_for_non_async_job(db, monkeypatch):
    from fastapi.testclient import TestClient

    import saas.jobs as jobs_mod
    from saas.api import app

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)

    client = TestClient(app)
    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms"}).json()["job_id"]
    status = client.get(f"/jobs/{job_id}/status").json()
    assert status["status"] == "draft"
    assert status["queue_job_id"] is None
    assert "queue_state" not in status  # no RQ lookup when there is no queue job
