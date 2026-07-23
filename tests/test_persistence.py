"""E2.2 persistence tests: DesignJob/agent serialize-rehydrate over SQLite (no OpenAI).

Verifies the JobRepository survives a "restart" and "crosses the worker boundary" by
using two repository instances that share one on-disk SQLite database (two independent
in-process caches = two processes). All numbers still originate from deterministic tools.
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from agents.certify import certify_candidate
from agents.design_candidate import candidate_from_tune_result
from agents.orchestrator import DesignSession
from agents.pid_tuner import tune_pid
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec
from saas.db import init_db, make_engine
from saas.jobs import DesignJob
from saas.models import ArtifactRow, DesignJobRow, MessageRow
from saas.repository import JobRepository
from saas.serialization import rehydrated_candidate, to_jsonable


@pytest.fixture()
def db(tmp_path):
    """A file-backed SQLite engine + session factory shared across repositories."""
    url = f"sqlite:///{tmp_path / 'copilot_test.db'}"
    engine = make_engine(url)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


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


def _completed_job(job_id: str = "job-done") -> DesignJob:
    """A realistic completed job (real scorecard + session_dict + certification)."""
    spec = _easy_spec()
    real = tune_pid(spec, method="grid", grid={"Kp": [100.0], "Ki": [200.0], "Kd": [10.0]})
    cand = candidate_from_tune_result(real)
    session = DesignSession(
        nl_spec="easy",
        mode="script",
        spec=spec,
        status="passed",
        best=cand,
        rationale="Grounded rationale.",
        total_wall_time_s=0.1,
        total_tool_evaluations=1,
        total_tokens=0,
    )
    job = DesignJob(job_id=job_id, plant_id="dc_motor_ctms", mode="script")
    job.nl_spec = "easy"
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.spec_confirmed = True
    job._session = session
    job.session_dict = session.to_dict(include_scorecard_json=False)
    job.scorecard = cand.scorecard
    job.certification = certify_candidate(cand).to_dict()
    job.status = "completed"
    return job


# --------------------------------------------------------------------------- #
# Record round-trip
# --------------------------------------------------------------------------- #
def test_job_record_roundtrip_preserves_public_state():
    job = _completed_job()
    record = to_jsonable(job.to_record())
    restored = DesignJob.from_record(record)
    # Durable public state matches; live objects are intentionally dropped.
    assert restored.to_public_dict() == job.to_public_dict()
    assert restored._session is None
    assert restored._spec is None


def test_to_jsonable_strips_numpy_and_nonfinite():
    payload = {
        "arr": np.array([1.0, 2.0, 3.0]),
        "f": np.float64(1.5),
        "i": np.int64(7),
        "b": np.bool_(True),
        "nan": float("nan"),
        "inf": float("inf"),
        "nested": [np.array([0.0, np.nan])],
    }
    out = to_jsonable(payload)
    assert out["arr"] == [1.0, 2.0, 3.0]
    assert out["f"] == 1.5 and isinstance(out["f"], float)
    assert out["i"] == 7 and isinstance(out["i"], int)
    assert out["b"] is True
    assert out["nan"] is None and out["inf"] is None
    assert out["nested"] == [[0.0, None]]


# --------------------------------------------------------------------------- #
# Repository persistence + cross-process rehydrate
# --------------------------------------------------------------------------- #
def test_repository_persist_and_rehydrate_in_fresh_process(db):
    writer = JobRepository(session_factory=db)
    job = writer.create(plant_id="dc_motor_ctms", mode="heuristic")
    job.motor_dict = {
        "name": "m1",
        "source": "manual",
        "params": {"J": 0.01, "b": 0.1, "K": 0.01, "R": 1.0, "L": 0.5},
        "V_max": 12.0,
        "characteristics": {"omega_max_rad_s": 2.3},
        "warnings": [],
    }
    job.motor_confirmed = True
    job.chat.append({"role": "user", "content": "hi"})
    writer.save(job)

    # Fresh repository over the SAME DB = a new process with an empty cache.
    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got is not job  # genuinely rehydrated, not the cached object
    assert got.motor_dict["name"] == "m1"
    assert got.motor_confirmed is True
    assert got.chat[-1]["content"] == "hi"
    assert got.tenant_id == "dev"


def test_rehydrated_job_rebuilds_live_motor_params(db):
    from saas.service import effective_motor_params

    writer = JobRepository(session_factory=db)
    job = writer.create(plant_id="dc_motor_ctms")
    job.motor_dict = {
        "name": "m2",
        "source": "manual",
        "params": {"J": 0.02, "b": 0.2, "K": 0.02, "R": 2.0, "L": 0.3},
        "V_max": 24.0,
    }
    writer.save(job)

    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got._motor is None  # not yet materialized
    params = effective_motor_params(got)  # lazily rebuilds from motor_dict
    assert params.J == pytest.approx(0.02)
    assert params.R == pytest.approx(2.0)


def test_rev_bump_lets_stale_cache_detect_worker_update(db):
    api = JobRepository(session_factory=db)
    worker = JobRepository(session_factory=db)

    job = api.create(plant_id="dc_motor_ctms")
    cached = api.get(job.job_id)  # warms the api cache
    assert cached.status == "draft"

    # Worker (separate cache) picks up the job and completes it.
    wjob = worker.get(job.job_id)
    wjob.status = "completed"
    wjob.scorecard = {"summary": {"all_constraints_pass": True}, "scenarios": []}
    worker.save(wjob)

    # API must notice the newer rev and rehydrate instead of serving stale draft state.
    refreshed = api.get(job.job_id)
    assert refreshed.status == "completed"
    assert refreshed.scorecard["summary"]["all_constraints_pass"] is True


def test_completed_job_survives_rehydrate_with_scorecard(db):
    writer = JobRepository(session_factory=db)
    job = _completed_job(job_id=writer.create().job_id)
    writer.save(job)

    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got.status == "completed"
    assert got.scorecard["summary"]["n_scenarios"] == 1
    # numpy trajectories became plain JSON lists.
    traj = got.scorecard["scenarios"][0]["trajectories"]["omega"]
    assert isinstance(traj, list) and all(isinstance(x, (int, float)) for x in traj)


# --------------------------------------------------------------------------- #
# Agent transcript persistence
# --------------------------------------------------------------------------- #
def test_agent_snapshot_persists_and_restores(db):
    from agents.design_agent import DesignAgentSession

    writer = JobRepository(session_factory=db)
    job = writer.create()
    agent = DesignAgentSession(job=job, model="test-model")
    agent.messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    agent.tool_log = [{"tool": "define_plant", "args": {}, "result": {"motor": "m"}}]
    agent.total_tokens = 42
    job._agent = agent
    writer.save(job)

    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got.agent_state["total_tokens"] == 42

    rehydrated = DesignAgentSession(job=got, model=None).restore(got.agent_state)
    assert rehydrated.total_tokens == 42
    assert rehydrated.messages[-1]["content"] == "hi"
    assert rehydrated.tool_log[0]["tool"] == "define_plant"
    assert rehydrated.model == "test-model"


# --------------------------------------------------------------------------- #
# Export across the process boundary (no live controller)
# --------------------------------------------------------------------------- #
def test_export_from_rehydrated_job_without_live_session(db, tmp_path):
    writer = JobRepository(session_factory=db)
    job = _completed_job(job_id=writer.create().job_id)
    writer.save(job)

    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got._session is None  # no live controller after rehydrate

    stub = rehydrated_candidate(got)
    assert stub is not None
    assert stub.kind == "pid"
    assert {"Kp", "Ki", "Kd"} <= set(stub.params)

    from saas.service import export_job

    path = export_job(got, out_dir=tmp_path / "exports")
    assert path.exists()


# --------------------------------------------------------------------------- #
# Normalized projections written through
# --------------------------------------------------------------------------- #
def test_projections_written_for_messages_and_artifacts(db):
    writer = JobRepository(session_factory=db)
    job = _completed_job(job_id=writer.create().job_id)
    job.chat = [
        {"role": "user", "content": "define my motor"},
        {"role": "assistant", "content": "done"},
    ]
    writer.save(job)

    with db() as session:
        msgs = session.query(MessageRow).filter_by(job_id=job.job_id).order_by(MessageRow.seq).all()
        assert [m.role for m in msgs] == ["user", "assistant"]
        kinds = {a.kind for a in session.query(ArtifactRow).filter_by(job_id=job.job_id).all()}
        assert {"spec", "certification"} <= kinds
        row = session.get(DesignJobRow, job.job_id)
        assert row.rev >= 1
        assert row.status == "completed"


# --------------------------------------------------------------------------- #
# Service layer persists through the active store (end-to-end _save wiring)
# --------------------------------------------------------------------------- #
def test_service_layer_persists_confirm_and_run(db, monkeypatch):
    """confirm_and_run through the repo store must survive a fresh-process rehydrate."""
    from unittest.mock import patch

    import saas.jobs as jobs_mod
    from saas import service

    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)

    spec = _easy_spec()
    job = service.create_job(plant_id="dc_motor_ctms", mode="script")
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.nl_spec = "easy"
    job.status = "spec_ready"

    real = tune_pid(spec, method="grid", grid={"Kp": [100.0], "Ki": [200.0], "Kd": [10.0]})
    cand = candidate_from_tune_result(real)
    fake_session = DesignSession(
        nl_spec="easy", mode="script", spec=spec, status="passed", best=cand,
        rationale="ok", total_wall_time_s=0.1, total_tool_evaluations=1, total_tokens=0,
    )
    with patch("saas.service.run_design_session", return_value=fake_session):
        service.confirm_and_run(job, max_iterations=1)

    assert job.status == "completed"

    # Fresh process: rehydrate from the shared DB and confirm the result persisted.
    reader = JobRepository(session_factory=db)
    got = reader.get(job.job_id)
    assert got.status == "completed"
    assert got.certification is not None
    assert got.scorecard["summary"]["all_constraints_pass"] in (True, False)
