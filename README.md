# Agentic Orchestration of DC Motor Control

Step-by-step labs building from a DC motor plant model to agentic PID orchestration (simulation first; hardware later).

## Setup (uv)

```powershell
# From the project root
uv sync

# Register the venv kernel for Jupyter (once)
uv run python -m ipykernel install --user --name=agentic-dc-motor --display-name="Python (agentic-dc-motor)"
```

Open notebooks with the **Python (agentic-dc-motor)** kernel (project root must be on `PYTHONPATH` / cwd so `import dc_motor` works).

## OpenAI API key (later labs)

```powershell
copy .env.example .env
# Edit .env and set OPENAI_API_KEY=...
```

Load with `python-dotenv` when agent code is added. Do not commit `.env`.

## Current labs

| Notebook | Focus |
|----------|--------|
| `Lab_01.ipynb` | CTMS plant + open-loop + closed-loop PID + metrics |
| `Lab_02.ipynb` | Shared evaluation harness / scorecard (`dc_motor` package) |

Package: `dc_motor/` — plant, `PIDController`, scenarios, `evaluate_controller`, JSON scorecard export.

Roadmap: `PROJECT_SEQUENCE.txt`.

## Dependencies

Managed in `pyproject.toml` via [uv](https://docs.astral.sh/uv/). Runtime: `numpy`, `scipy`, `matplotlib`, `openai`, `python-dotenv`. Dev: `ipykernel`, `jupyter`.
