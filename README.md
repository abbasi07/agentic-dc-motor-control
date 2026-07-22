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
| `dc_motor/` | Plant, controllers, metrics, scenarios, eval, specs, `FailureDigest`, **plant registry** |
| `agents/` | Spec agent, PID tuner, orchestrator, specialists, certification / export |
| `experiments/` | Ablation suite (`run_ablation`, comparison table) |
| `saas/` | FastAPI jobs API + Streamlit UI + clarify/feedback |
| `examples/` | Runnable CLI demos (`lab_01` … `lab_08`) |
| `tests/` | Smoke / unit tests (mocked Spec Interpreter; no OpenAI required) |

Roadmap / decisions: `PROJECT_SEQUENCE.txt`.

Plant registry IDs: `dc_motor_ctms`, `first_order_lag`, `position_servo`.

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
- `agents.certify_candidate`, `export_certified_package`
- `experiments.run_ablation`, `ablation_comparison_table`
- `saas.api:app` job endpoints under `/jobs` (incl. `POST /jobs/{id}/agent` chat-first agent)

## Dependencies

Managed in `pyproject.toml` via [uv](https://docs.astral.sh/uv/). Runtime: `numpy`, `scipy`, `matplotlib`, `openai`, `python-dotenv`, `fastapi`, `uvicorn`, `streamlit`. Dev: `pytest`, `httpx`.
