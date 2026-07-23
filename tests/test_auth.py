"""E2.5 auth / multi-tenant / rate-limit / budget tests (no real Redis, no OpenAI).

Everything runs over SQLite (:class:`AuthManager` + :class:`JobRepository`) and
``fakeredis`` (:class:`RateLimiter`). We verify:
- API keys are hashed (never stored raw), deterministic to verify, and scoped to a tenant;
- the dev bootstrap key seeds idempotently;
- ``JobRepository`` get/list are scoped by tenant;
- the FastAPI ``/jobs`` routes require a Bearer key when auth is enabled (401), forbid
  cross-tenant access (404), and pass through when auth is disabled (dev tenant);
- the Redis rate limiter allows up to the limit then blocks (429), and fails open;
- token / iteration budgets are enforced and surfaced read-only in the workspace.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import fakeredis
import pytest
from sqlalchemy.orm import sessionmaker

import saas.auth as auth_mod
import saas.jobs as jobs_mod
import saas.ratelimit as ratelimit_mod
from agents.design_candidate import candidate_from_tune_result
from agents.orchestrator import DesignSession
from agents.pid_tuner import tune_pid
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec
from saas.auth import AuthManager, BudgetExceeded, generate_api_key
from saas.db import init_db, make_engine
from saas.jobs import JobStore
from saas.models import ApiKey
from saas.ratelimit import RateLimiter
from saas.repository import JobRepository


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'copilot_auth.db'}"
    engine = make_engine(url)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def redis_conn():
    return fakeredis.FakeStrictRedis()


def _settings(monkeypatch, **over) -> None:
    from saas import config

    base = config.get_settings()
    monkeypatch.setattr(config, "get_settings", lambda: dataclasses.replace(base, **over))


def _h(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


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
    real = tune_pid(spec, method="grid", grid={"Kp": [100.0], "Ki": [200.0], "Kd": [10.0]})
    cand = candidate_from_tune_result(real)
    return DesignSession(
        nl_spec="easy", mode="script", spec=spec, status="passed", best=cand,
        rationale="ok", total_wall_time_s=0.1, total_tool_evaluations=1, total_tokens=0,
    )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_auth_disabled_by_default():
    from saas.config import Settings, get_settings

    assert isinstance(get_settings(), Settings)
    assert get_settings().auth_enabled is False


# --------------------------------------------------------------------------- #
# AuthManager: hashing + create/verify + seeding
# --------------------------------------------------------------------------- #
def test_hash_is_deterministic_and_hides_raw(db):
    mgr = AuthManager(session_factory=db, pepper="p")
    raw = generate_api_key()
    h = mgr.hash_key(raw)
    assert h == mgr.hash_key(raw)  # deterministic (indexed lookup works)
    assert len(h) == 64 and raw not in h
    # Pepper matters: a different server secret yields a different hash.
    assert AuthManager(session_factory=db, pepper="q").hash_key(raw) != h


def test_create_and_verify_api_key(db):
    mgr = AuthManager(session_factory=db, pepper="p")
    mgr.ensure_tenant("t1", "Tenant One")
    raw = mgr.create_api_key("t1", label="k1")
    assert raw.startswith("cdc_")

    assert mgr.verify_api_key(raw) == "t1"
    assert mgr.verify_api_key("not-a-key") is None
    assert mgr.verify_api_key(None) is None

    with db() as session:
        rows = session.query(ApiKey).all()
        assert len(rows) == 1
        assert rows[0].key_hash != raw  # raw key never persisted
        assert rows[0].last_used_at is not None  # verify touched it


def test_inactive_key_is_rejected(db):
    mgr = AuthManager(session_factory=db, pepper="p")
    mgr.ensure_tenant("t1")
    raw = mgr.create_api_key("t1")
    with db() as session:
        row = session.query(ApiKey).one()
        row.active = False
        session.commit()
    assert mgr.verify_api_key(raw) is None


def test_seed_dev_api_key_idempotent(db, monkeypatch):
    _settings(monkeypatch, dev_api_key="seed-key")
    mgr = AuthManager(session_factory=db, pepper="p")
    assert mgr.seed_dev_api_key() == "dev"
    assert mgr.seed_dev_api_key() == "dev"  # second call adds no duplicate
    assert mgr.verify_api_key("seed-key") == "dev"
    with db() as session:
        assert session.query(ApiKey).count() == 1


def test_seed_dev_api_key_noop_without_key(db, monkeypatch):
    _settings(monkeypatch, dev_api_key=None)
    mgr = AuthManager(session_factory=db, pepper="p")
    assert mgr.seed_dev_api_key() is None
    with db() as session:
        assert session.query(ApiKey).count() == 0


# --------------------------------------------------------------------------- #
# Repository tenant scoping
# --------------------------------------------------------------------------- #
def test_repository_get_and_list_scoped_by_tenant(db):
    repo = JobRepository(session_factory=db)
    repo.ensure_tenant("acme", "Acme")
    dev_job = repo.create(plant_id="dc_motor_ctms")  # dev tenant (auto-ensured)
    acme_job = repo.create(plant_id="dc_motor_ctms", tenant_id="acme")

    # Cross-tenant get is a KeyError (surfaced as 404 — no existence leak).
    with pytest.raises(KeyError):
        repo.get(dev_job.job_id, tenant_id="acme")
    assert repo.get(dev_job.job_id, tenant_id="dev").job_id == dev_job.job_id

    assert [j.job_id for j in repo.list_jobs(tenant_id="dev")] == [dev_job.job_id]
    assert [j.job_id for j in repo.list_jobs(tenant_id="acme")] == [acme_job.job_id]
    # Unscoped list still returns everything (worker / admin path).
    assert len(repo.list_jobs()) == 2


def test_jobstore_get_scoped_by_tenant():
    store = JobStore()
    job = store.create(plant_id="dc_motor_ctms", tenant_id="dev")
    with pytest.raises(KeyError):
        store.get(job.job_id, tenant_id="other")
    assert store.get(job.job_id, tenant_id="dev").job_id == job.job_id
    assert store.list_jobs(tenant_id="other") == []


# --------------------------------------------------------------------------- #
# Rate limiter (fakeredis)
# --------------------------------------------------------------------------- #
def test_rate_limiter_allows_then_blocks(redis_conn):
    rl = RateLimiter(connection=redis_conn)
    r1 = rl.check("t", limit=2)
    r2 = rl.check("t", limit=2)
    r3 = rl.check("t", limit=2)
    assert (r1.allowed, r1.count) == (True, 1)
    assert (r2.allowed, r2.count) == (True, 2)
    assert (r3.allowed, r3.count) == (False, 3)
    # A different tenant has an independent window.
    assert rl.check("other", limit=2).allowed is True


def test_rate_limiter_zero_limit_is_unlimited(redis_conn):
    rl = RateLimiter(connection=redis_conn)
    assert rl.check("t", limit=0).allowed is True


def test_rate_limiter_fails_open_on_broken_connection():
    class _Broken:
        def incr(self, *a, **k):
            raise ConnectionError("redis down")

    rl = RateLimiter(connection=_Broken())
    assert rl.check("t", limit=1).allowed is True


# --------------------------------------------------------------------------- #
# API routes: auth on/off + cross-tenant + rate limit + budget
# --------------------------------------------------------------------------- #
def _wire_auth(monkeypatch, db, *, pepper="test-pepper", **over):
    """Point the API at a test repo + auth manager over the SQLite ``db``."""
    repo = JobRepository(session_factory=db)
    monkeypatch.setattr(jobs_mod, "_STORE", repo)
    mgr = AuthManager(session_factory=db, pepper=pepper)
    monkeypatch.setattr(auth_mod, "_AUTH", mgr)
    over.setdefault("rate_limit_per_minute", 0)
    _settings(monkeypatch, auth_enabled=True, **over)
    return repo, mgr


def test_routes_require_valid_key_when_auth_enabled(db, monkeypatch):
    from fastapi.testclient import TestClient

    from saas.api import app

    _, mgr = _wire_auth(monkeypatch, db)
    mgr.ensure_tenant("dev", "Dev")
    raw = mgr.create_api_key("dev", label="t")

    client = TestClient(app)
    body = {"plant_id": "dc_motor_ctms"}
    assert client.post("/jobs", json=body).status_code == 401  # no header
    assert client.post("/jobs", json=body, headers=_h("bogus")).status_code == 401
    ok = client.post("/jobs", json=body, headers=_h(raw))
    assert ok.status_code == 200 and ok.json()["job_id"]


def test_cross_tenant_access_is_404(db, monkeypatch):
    from fastapi.testclient import TestClient

    from saas.api import app

    _, mgr = _wire_auth(monkeypatch, db)
    mgr.ensure_tenant("dev", "Dev")
    mgr.ensure_tenant("acme", "Acme")
    dev_key = mgr.create_api_key("dev")
    acme_key = mgr.create_api_key("acme")

    client = TestClient(app)
    job_id = client.post(
        "/jobs", json={"plant_id": "dc_motor_ctms"}, headers=_h(dev_key)
    ).json()["job_id"]

    assert client.get(f"/jobs/{job_id}", headers=_h(dev_key)).status_code == 200
    assert client.get(f"/jobs/{job_id}", headers=_h(acme_key)).status_code == 404
    assert client.get(f"/jobs/{job_id}/workspace", headers=_h(acme_key)).status_code == 404
    # List is tenant-scoped: acme sees none, dev sees its one job.
    assert client.get("/jobs", headers=_h(acme_key)).json()["jobs"] == []
    assert len(client.get("/jobs", headers=_h(dev_key)).json()["jobs"]) == 1


def test_auth_disabled_defaults_to_dev_tenant(monkeypatch):
    from fastapi.testclient import TestClient

    from saas.api import app

    store = JobStore()
    monkeypatch.setattr(jobs_mod, "_STORE", store)
    client = TestClient(app)  # auth disabled by default

    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms"}).json()["job_id"]
    assert store.get(job_id).tenant_id == "dev"
    assert client.get(f"/jobs/{job_id}").status_code == 200  # no header needed


def test_rate_limit_returns_429(db, redis_conn, monkeypatch):
    from fastapi.testclient import TestClient

    from saas.api import app

    _, mgr = _wire_auth(monkeypatch, db, rate_limit_per_minute=2)
    mgr.ensure_tenant("dev", "Dev")
    raw = mgr.create_api_key("dev")
    monkeypatch.setattr(ratelimit_mod, "_LIMITER", RateLimiter(connection=redis_conn))

    client = TestClient(app)
    assert client.get("/jobs", headers=_h(raw)).status_code == 200  # count=1
    assert client.get("/jobs", headers=_h(raw)).status_code == 200  # count=2
    resp = client.get("/jobs", headers=_h(raw))  # count=3 > 2
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #
def test_workspace_surfaces_budget_limits(monkeypatch):
    from fastapi.testclient import TestClient

    from saas.api import app

    monkeypatch.setattr(jobs_mod, "_STORE", JobStore())
    _settings(
        monkeypatch,
        max_tokens_per_session=1000,
        max_design_iterations=7,
        rate_limit_per_minute=30,
    )
    client = TestClient(app)
    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms"}).json()["job_id"]
    budgets = client.get(f"/jobs/{job_id}/workspace").json()["budgets"]
    assert budgets["max_tokens_per_session"] == 1000
    assert budgets["max_design_iterations"] == 7
    assert budgets["rate_limit_per_minute"] == 30
    assert budgets["tokens_remaining"] == 1000


def test_agent_chat_blocks_when_token_budget_exhausted(monkeypatch):
    from saas import service

    monkeypatch.setattr(jobs_mod, "_STORE", JobStore())
    _settings(monkeypatch, max_tokens_per_session=100)

    job = service.create_job(plant_id="dc_motor_ctms")
    job.agent_state = {"total_tokens": 150}  # already over budget
    with pytest.raises(BudgetExceeded):
        service.agent_chat(job, "hi")  # no OpenAI call is ever reached


def test_agent_route_returns_429_on_budget(monkeypatch):
    from fastapi.testclient import TestClient

    from saas.api import app

    store = JobStore()
    monkeypatch.setattr(jobs_mod, "_STORE", store)
    _settings(monkeypatch, max_tokens_per_session=50)

    client = TestClient(app)
    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms"}).json()["job_id"]
    job = store.get(job_id)
    job.agent_state = {"total_tokens": 60}
    store.save(job)

    resp = client.post(f"/jobs/{job_id}/agent", json={"message": "hi"})
    assert resp.status_code == 429


def test_confirm_and_run_caps_iterations_to_budget(monkeypatch):
    from saas import service

    monkeypatch.setattr(jobs_mod, "_STORE", JobStore())
    _settings(monkeypatch, max_design_iterations=3)

    spec = _easy_spec()
    job = service.create_job(plant_id="dc_motor_ctms", mode="script")
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.nl_spec = "easy"
    job.status = "spec_ready"

    captured: dict[str, int | None] = {}

    def _fake_run(nl_spec, **kwargs):
        captured["max_iterations"] = kwargs.get("max_iterations")
        return _fake_session(spec)

    with patch("saas.service.run_design_session", side_effect=_fake_run):
        service.confirm_and_run(job, max_iterations=10)  # asks for 10, capped to 3

    assert captured["max_iterations"] == 3
