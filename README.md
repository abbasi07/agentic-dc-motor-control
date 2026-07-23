# Agentic Orchestration of DC Motor Control

Adaptive control-design agent for a DC motor digital twin (**simulation / SaaS only** — no hardware).
Natural-language specs → formal constraints → iterative redesign → certified export package.

## Setup (uv)

```powershell
# From the project root
uv sync
```

## OpenAI API key (examples 03+ / Copilot)

```powershell
copy .env.example .env
# Edit .env and set OPENAI_API_KEY=...
```

Default model: `OPENAI_MODEL=gpt-5.4-nano`. Do not commit `.env`.

NL → `DesignSpec` and LLM orchestrator actions are **OpenAI-only** (no regex fallback).
If the key is missing, examples/API print a clear message and fail.

## Full stack (Docker Compose + React UI)

The production-shaped stack — Postgres 16, Redis 7, the FastAPI API, the RQ worker, and
the **React/Next two-pane UI** — runs under Docker Compose:

```powershell
copy .env.example .env    # then set OPENAI_API_KEY (the chat agent is OpenAI-only)
docker compose up         # db + redis + api + worker + web
```

- **Web UI:** http://localhost:3000 — chat-first controller design. RIGHT pane = chat +
  live agent activity; LEFT pane = dynamic artifact tabs (Motor · Requirements ·
  Feasibility · Results & Plots · Export) that appear as the conversation reaches each
  stage. Trajectories render client-side; the UI streams `GET /jobs/{id}/events` over SSE
  and sends `Authorization: Bearer <key>`.
- **API:** http://localhost:8000 (`/docs` = Swagger). Persistence + async runs + live
  events + auth are all **on** in Compose. The dev bootstrap key `dev-local-key` is
  seeded on startup (override `COPILOT_DEV_API_KEY` / `COPILOT_API_KEY_PEPPER` in `.env`).
- **Design flow:** the LLM plans and talks; deterministic tools compute every number; the
  workspace the UI renders is reflect-only (the LLM never authors UI or event types).

Run the web app on its own (host) against a running API:

```powershell
cd web
copy .env.example .env.local
npm install
npm run dev   # http://localhost:3000
```

## Design Copilot (local SaaS MVP)

Hybrid chat + locked DesignSpec panel + results/export. Tools compute metrics; chat only plans and explains.

```powershell
# UI (primary)
uv run streamlit run saas/ui_streamlit.py
# → http://localhost:8501

# Optional API (Swagger at /docs is NOT the GUI)
uv run uvicorn saas.api:app --host 127.0.0.1 --port 8000
```

Job flow: create job → interpret NL → clarify if needed → confirm & design → feedback/rerun → export zip (certification gate).

### Chat-first Design Agent (workstream D)

`agents.DesignAgentSession` is an OpenAI function-calling loop that owns a session and
exposes the engine as typed tools (`define_plant`, `set_spec`, `check_feasibility`,
`design_controller(type)`, `simulate`, `query_results`, `modify`, `export`). The model
plans and talks; **tools compute every number**. `query_results` is deterministic and
answers strictly from the stored scorecard — it can never invent a metric. Reachable via
`POST /jobs/{id}/agent` (OpenAI-only). Define ANY DC motor in chat, pick a controller
type, and ask follow-ups like *"what was the settling time?"* in one conversation.

## Package layout

| Path | Role |
|------|------|
| `dc_motor/` | Plant, controllers, **state-space model**, metrics, scenarios, eval, specs, `FailureDigest`, **plant registry** |
| `agents/` | Spec agent, PID tuner, orchestrator, **controller registry**, specialist designers, advanced controllers, **critic**, certification / export |
| `experiments/` | Ablation suite (`run_ablation`, comparison table) |
| `saas/` | FastAPI jobs API + persistence/queue/events/auth + Streamlit (frozen) |
| `web/` | React/Next two-pane UI (E3): chat + live activity, dynamic artifact tabs |
| `examples/` | Runnable CLI demos (`lab_01` … `lab_08`) |
| `tests/` | Smoke / unit tests (mocked Spec Interpreter; no OpenAI required) |

Roadmap / decisions: `PROJECT_SEQUENCE.txt`.

Plant registry IDs: `dc_motor_ctms`, `first_order_lag`, `position_servo`.

### Controller families (pluggable registry)

`agents/controller_registry.py` maps each controller *kind* to a designer + metadata
(the `design_controller(type=…)` name, the orchestrator `call_*` action, human labels,
and the `FailureDigest` tags it addresses). All controllers share
`reset()` / `step(measurement, reference, dt) -> u` and are scored by
`evaluate_controller`.

| Type | Kind | Method | Library |
|------|------|--------|---------|
| `pid` | `pid` | Constraint-aware PID (grid + differential evolution) | scipy |
| `robust` | `robust_pid` | Mismatch-focused detuned PID | scipy |
| `lqr` | `lqr` | Integral-augmented LQR + Luenberger observer | python-control |
| `lqg` | `lqg` | Integral-augmented LQR + Kalman filter | python-control |
| `mpc` | `mpc` | Constrained receding-horizon QP (offset-free) | cvxpy + OSQP |
| `mrac` / `adaptive` | `mrac` | Lyapunov model-reference adaptive control | numpy |
| `fuzzy` | `fuzzy_pid` | Takagi–Sugeno fuzzy gain-scheduling PID | numpy |

Each family is also an orchestrator action (`call_lqr`, `call_lqg`, `call_mpc`,
`call_mrac`, `call_fuzzy`, `call_robust`), so the adaptive redesign loop can switch
*structure* — not just retune gains — when a failure pattern calls for it. A grounded
`agents/critic.py` translates the `FailureDigest` into an ordered action recommendation
(never inventing numbers).

## Run examples

```powershell
uv run python examples/lab_01_plant_pid.py
uv run python examples/lab_02_evaluation.py
uv run python examples/lab_03_spec_agent.py
uv run python examples/lab_08_ablation.py --modes script heuristic
```

## Tests

```powershell
uv sync --group dev
uv run pytest -q
```

## Public APIs (stable)

- `dc_motor.evaluate_controller`, `list_plants`, `get_plant_spec`, `check_feasibility`
- `agents.interpret_spec`, `interpret_plant`, `tune_pid`, `run_design_session` (optional `spec=`, `plant_id=`)
- `agents.DesignAgentSession` — chat-first tool-calling Design Agent (workstream D);
  deterministic tools + grounded `query_results` (never invents numbers)
- `agents.design_by_type`, `CONTROLLER_FAMILIES`, `registry_metadata` — pluggable
  controller registry (workstream C); `agents.diagnose` — grounded design critic
- `agents.certify_candidate`, `export_certified_package`
- `experiments.run_ablation`, `ablation_comparison_table`
- `saas.api:app` job endpoints under `/jobs` (incl. `POST /jobs/{id}/agent` chat-first agent)

## Dependencies

Managed in `pyproject.toml` via [uv](https://docs.astral.sh/uv/). Runtime: `numpy`, `scipy`, `control` (python-control, for LQR/LQG/Kalman), `cvxpy` + `osqp` (constrained MPC QP), `matplotlib`, `openai`, `python-dotenv`, `fastapi`, `uvicorn`, `streamlit`. Dev: `pytest`, `httpx`.
