"""FastAPI smoke tests (TestClient)."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

import saas.jobs as jobs_mod
from saas.api import app
from saas.jobs import JobStore


def test_health_and_plants():
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    plants = client.get("/plants").json()["plants"]
    assert any(p["plant_id"] == "dc_motor_ctms" for p in plants)


def test_create_and_get_job():
    jobs_mod._STORE = JobStore()
    client = TestClient(app)
    created = client.post(
        "/jobs", json={"plant_id": "dc_motor_ctms", "mode": "heuristic"}
    ).json()
    job_id = created["job_id"]
    got = client.get(f"/jobs/{job_id}").json()
    assert got["job_id"] == job_id
    assert got["status"] == "draft"


def test_agent_chat_route_wires_tool_agent():
    jobs_mod._STORE = JobStore()
    client = TestClient(app)
    job_id = client.post("/jobs", json={"plant_id": "dc_motor_ctms"}).json()["job_id"]

    def _fake_chat(self, message, **kwargs):  # noqa: ANN001
        reply = "Tool-grounded reply."
        self.job.chat.append({"role": "user", "content": message})
        self.job.chat.append({"role": "assistant", "content": reply})
        return reply

    with patch("agents.design_agent.DesignAgentSession.chat", _fake_chat):
        out = client.post(
            f"/jobs/{job_id}/agent", json={"message": "design a fast PID"}
        ).json()

    roles = [m["role"] for m in out["chat"]]
    assert roles[-2:] == ["user", "assistant"]
    assert out["chat"][-1]["content"] == "Tool-grounded reply."
