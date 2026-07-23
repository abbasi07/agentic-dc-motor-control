"""E2.4 live-event tests: Redis pub/sub fan-out + SSE endpoint (no real Redis/OpenAI).

Everything runs over ``fakeredis`` and the in-memory ``JobStore``. We verify:
- the :class:`saas.events.EventBus` publish/subscribe round-trip + best-effort publish;
- the service publishes ``run.status`` transitions (queued/running/completed) +
  ``workspace.updated`` from the (inline) design-run path — the SAME calls the RQ worker
  makes, so a connected client sees progress regardless of which process produced it;
- the Design Agent emits ``tool.started``/``tool.finished``/``workspace.updated`` around
  every tool and ``refusal`` for an off-topic turn (deterministic, no OpenAI), plus
  ``message.delta`` on a final assistant reply (OpenAI client mocked);
- the SSE route is bounded where it must be (404 unknown job, 503 when events disabled)
  and its async generator emits the initial workspace snapshot.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any
from unittest.mock import patch

import anyio
import fakeredis
import pytest
from rq import Queue

from agents.design_agent import DesignAgentSession
from agents.design_candidate import candidate_from_tune_result
from agents.orchestrator import DesignSession
from agents.pid_tuner import tune_pid
from dc_motor.specs import DesignSpec, validate_and_clamp_design_spec
from saas.events import (
    EVENT_MESSAGE_DELTA,
    EVENT_REFUSAL,
    EVENT_RUN_STATUS,
    EVENT_TOOL_FINISHED,
    EVENT_TOOL_STARTED,
    EVENT_WORKSPACE_UPDATED,
    EventBus,
    get_event_bus,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
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


def _drain(pubsub, *, max_polls: int = 100, timeout: float = 0.05) -> list[dict[str, Any]]:
    """Collect already-buffered events from a subscribed pubsub.

    Publishing is synchronous (done before we drain), but ``get_message`` can return
    ``None`` for a swallowed subscribe-confirmation before real messages surface, so we
    tolerate a few misses instead of stopping on the first ``None``.
    """
    from saas.events import _decode_message

    out: list[dict[str, Any]] = []
    misses = 0
    for _ in range(max_polls):
        message = pubsub.get_message(timeout=timeout)
        if message is None:
            misses += 1
            if misses >= 3:
                break
            continue
        misses = 0
        event = _decode_message(message)
        if event is not None:
            out.append(event)
    return out


def _spec_ready_job(store):
    spec = _easy_spec()
    job = store.create(plant_id="dc_motor_ctms", mode="script")
    job.nl_spec = "easy"
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.status = "spec_ready"
    store.save(job)
    return job, spec


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_events_disabled_by_default():
    from saas.config import Settings, get_settings

    assert isinstance(get_settings(), Settings)
    assert get_settings().events_enabled is False
    # Disabled => no bus (publishing is a no-op).
    assert get_event_bus() is None


# --------------------------------------------------------------------------- #
# EventBus plumbing
# --------------------------------------------------------------------------- #
def test_eventbus_publish_subscribe_roundtrip():
    fake = fakeredis.FakeStrictRedis()
    bus = EventBus(connection=fake)
    job_id = "job-1"

    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(bus.channel(job_id))

    event = bus.publish(job_id, EVENT_RUN_STATUS, {"status": "running"})
    assert event is not None
    assert event["type"] == EVENT_RUN_STATUS
    assert event["job_id"] == job_id

    received = _drain(pubsub)
    assert len(received) == 1
    assert received[0]["type"] == EVENT_RUN_STATUS
    assert received[0]["data"]["status"] == "running"


def test_eventbus_publish_coerces_non_jsonable_and_is_best_effort():
    import numpy as np

    fake = fakeredis.FakeStrictRedis()
    bus = EventBus(connection=fake)
    job_id = "job-2"
    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(bus.channel(job_id))

    # numpy + NaN in the payload must round-trip (numpy->python, NaN->None).
    bus.publish(job_id, EVENT_WORKSPACE_UPDATED, {"x": np.float64(1.5), "bad": float("nan")})
    received = _drain(pubsub)
    assert received[0]["data"]["x"] == 1.5
    assert received[0]["data"]["bad"] is None


def test_eventbus_publish_never_raises_on_broken_connection():
    class _BrokenConn:
        def publish(self, *a, **k):
            raise ConnectionError("redis down")

    bus = EventBus(connection=_BrokenConn())
    # Best-effort: returns None, does not propagate the error.
    assert bus.publish("job-3", EVENT_RUN_STATUS, {"status": "running"}) is None


def test_eventbus_listen_yields_events():
    fake = fakeredis.FakeStrictRedis()
    bus = EventBus(connection=fake)
    job_id = "job-4"

    # Subscribe first (pub/sub has no backlog), then publish, then read it back off the
    # same subscription EventBus.subscribe() hands to the SSE endpoint.
    pubsub = bus.subscribe(job_id)
    bus.publish(job_id, EVENT_TOOL_STARTED, {"tool": "set_spec"})
    try:
        events = _drain(pubsub)
    finally:
        pubsub.close()
    assert len(events) == 1
    assert events[0]["type"] == EVENT_TOOL_STARTED
    assert events[0]["data"]["tool"] == "set_spec"


# --------------------------------------------------------------------------- #
# Service: run.status transitions + workspace.updated (worker + inline share these)
# --------------------------------------------------------------------------- #
def test_confirm_and_run_publishes_status_transitions(monkeypatch):
    import saas.jobs as jobs_mod
    from saas import service
    from saas.jobs import JobStore

    store = JobStore()
    monkeypatch.setattr(jobs_mod, "_STORE", store)

    fake = fakeredis.FakeStrictRedis()
    bus = EventBus(connection=fake)
    monkeypatch.setattr(service, "get_event_bus", lambda: bus)

    job, spec = _spec_ready_job(store)
    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(bus.channel(job.job_id))

    with patch("saas.service.run_design_session", return_value=_fake_session(spec)):
        service.confirm_and_run(job, max_iterations=1)

    events = _drain(pubsub)
    types = [e["type"] for e in events]
    statuses = [e["data"]["status"] for e in events if e["type"] == EVENT_RUN_STATUS]

    assert EVENT_RUN_STATUS in types
    assert "running" in statuses
    assert "completed" in statuses
    assert EVENT_WORKSPACE_UPDATED in types
    # The completion workspace carries the results artifact (scorecard present).
    ws_events = [e for e in events if e["type"] == EVENT_WORKSPACE_UPDATED]
    assert any("results" in e["data"].get("artifacts", {}) for e in ws_events)


def test_confirm_and_run_publishes_failed_on_error(monkeypatch):
    import saas.jobs as jobs_mod
    from saas import service
    from saas.jobs import JobStore

    store = JobStore()
    monkeypatch.setattr(jobs_mod, "_STORE", store)
    fake = fakeredis.FakeStrictRedis()
    bus = EventBus(connection=fake)
    monkeypatch.setattr(service, "get_event_bus", lambda: bus)

    job, _ = _spec_ready_job(store)
    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(bus.channel(job.job_id))

    with patch("saas.service.run_design_session", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            service.confirm_and_run(job, max_iterations=1)

    events = _drain(pubsub)
    statuses = [e["data"]["status"] for e in events if e["type"] == EVENT_RUN_STATUS]
    assert "failed" in statuses
    assert any(e["type"] == "error" for e in events)


def test_enqueue_publishes_queued(monkeypatch):
    import saas.jobs as jobs_mod
    from saas import service
    from saas.jobs import JobStore

    store = JobStore()
    monkeypatch.setattr(jobs_mod, "_STORE", store)
    fake = fakeredis.FakeStrictRedis()
    bus = EventBus(connection=fake)
    monkeypatch.setattr(service, "get_event_bus", lambda: bus)

    job, _ = _spec_ready_job(store)
    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(bus.channel(job.job_id))

    # Async queue with no worker: the job stays queued and only that event fires.
    q = Queue("copilot", connection=fake, is_async=True)
    service.enqueue_design_run(job, queue=q)

    events = _drain(pubsub)
    statuses = [e["data"]["status"] for e in events if e["type"] == EVENT_RUN_STATUS]
    assert statuses == ["queued"]


# --------------------------------------------------------------------------- #
# Design Agent: tool.* / workspace.updated / refusal / message.delta
# --------------------------------------------------------------------------- #
def test_agent_tool_events_around_dispatch():
    session = DesignAgentSession.create(plant_id="dc_motor_ctms")
    session.load_spec(_easy_spec())
    fake = fakeredis.FakeStrictRedis()
    session.events = EventBus(connection=fake)

    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(session.events.channel(session.job.job_id))

    session._dispatch_tool("check_feasibility", "{}")

    events = _drain(pubsub)
    types = [e["type"] for e in events]
    assert types == [EVENT_TOOL_STARTED, EVENT_TOOL_FINISHED, EVENT_WORKSPACE_UPDATED]
    assert events[0]["data"]["tool"] == "check_feasibility"
    assert events[1]["data"]["tool"] == "check_feasibility"
    assert "feasible" in events[1]["data"]["result"]


def test_agent_refusal_event_no_openai():
    session = DesignAgentSession.create(plant_id="dc_motor_ctms")
    fake = fakeredis.FakeStrictRedis()
    session.events = EventBus(connection=fake)

    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(session.events.channel(session.job.job_id))

    reply = session.chat("tell me a joke about elephants")
    assert "control-design copilot" in reply.lower()

    events = _drain(pubsub)
    assert [e["type"] for e in events] == [EVENT_REFUSAL]
    assert events[0]["data"]["content"] == reply


def test_agent_message_delta_event(monkeypatch):
    # Mock the OpenAI client so chat() returns a plain assistant message (no tools).
    class _Msg:
        content = "Here is a summary from the tools."
        tool_calls = None

    class _Choice:
        message = _Msg()

    class _Usage:
        total_tokens = 7

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.chat = _Chat()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr("openai.OpenAI", _FakeClient)

    session = DesignAgentSession.create(plant_id="dc_motor_ctms")
    fake = fakeredis.FakeStrictRedis()
    session.events = EventBus(connection=fake)
    pubsub = fake.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(session.events.channel(session.job.job_id))

    reply = session.chat("What settling time can a PID achieve on this motor?")
    assert reply == "Here is a summary from the tools."

    events = _drain(pubsub)
    delta = [e for e in events if e["type"] == EVENT_MESSAGE_DELTA]
    assert len(delta) == 1
    assert delta[0]["data"]["content"] == "Here is a summary from the tools."
    assert delta[0]["data"]["final"] is True


# --------------------------------------------------------------------------- #
# SSE route
# --------------------------------------------------------------------------- #
def _events_settings(monkeypatch, enabled: bool) -> None:
    from saas import config

    base = config.get_settings()
    monkeypatch.setattr(
        config, "get_settings", lambda: dataclasses.replace(base, events_enabled=enabled)
    )


def test_sse_route_404_for_unknown_job(monkeypatch):
    from fastapi.testclient import TestClient

    import saas.jobs as jobs_mod
    from saas.api import app
    from saas.jobs import JobStore

    monkeypatch.setattr(jobs_mod, "_STORE", JobStore())
    _events_settings(monkeypatch, True)

    client = TestClient(app)
    resp = client.get("/jobs/does-not-exist/events")
    assert resp.status_code == 404


def test_sse_route_503_when_events_disabled(monkeypatch):
    from fastapi.testclient import TestClient

    import saas.jobs as jobs_mod
    from saas.api import app
    from saas.jobs import JobStore

    store = JobStore()
    monkeypatch.setattr(jobs_mod, "_STORE", store)
    _events_settings(monkeypatch, False)

    client = TestClient(app)
    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms"}).json()["job_id"]
    resp = client.get(f"/jobs/{job_id}/events")
    assert resp.status_code == 503


def test_sse_generator_emits_initial_snapshot_then_stops():
    from saas.api import _job_event_stream

    fake = fakeredis.FakeStrictRedis()
    bus = EventBus(connection=fake)
    job_id = "job-sse"
    initial = bus.build_event(job_id, EVENT_WORKSPACE_UPDATED, {"phase": "greeting"})

    class _Req:
        async def is_disconnected(self) -> bool:
            return True  # disconnect right after the initial yield

    async def _drive() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        async for item in _job_event_stream(bus, job_id, _Req(), initial):
            out.append(item)
        return out

    out = anyio.run(_drive)
    assert len(out) == 1
    assert out[0]["event"] == EVENT_WORKSPACE_UPDATED
    payload = json.loads(out[0]["data"])
    assert payload["data"]["phase"] == "greeting"
