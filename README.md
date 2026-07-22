# Agentic Orchestration of DC Motor Control

Step-by-step labs building from a DC motor plant model to agentic PID orchestration (simulation first; hardware later).

## Setup (uv)

```powershell
# From the project root
uv sync

# Register the venv kernel for Jupyter (once)
uv run python -m ipykernel install --user --name=agentic-dc-motor --display-name="Python (agentic-dc-motor)"
```

Open `Lab_01.ipynb` and select the **Python (agentic-dc-motor)** kernel.

## OpenAI API key (later labs)

```powershell
copy .env.example .env
# Edit .env and set OPENAI_API_KEY=...
```

Load with `python-dotenv` when agent code is added. Do not commit `.env`.

## Current labs

| Notebook | Focus |
|----------|--------|
| `Lab_01.ipynb` | DC motor parameters (CTMS) + open-loop speed step response |

## Dependencies

Managed in `pyproject.toml` via [uv](https://docs.astral.sh/uv/). Runtime: `numpy`, `scipy`, `matplotlib`, `openai`, `python-dotenv`. Dev: `ipykernel`, `jupyter`.
