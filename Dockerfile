# Control Design Copilot — backend image (shared by the `api` and `worker` services).
# Simulation-only SaaS backend: FastAPI + RQ worker over Postgres + Redis.
FROM python:3.12-slim

# uv (Astral) provides fast, reproducible dependency management.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Keep the virtualenv OUTSIDE /app so a dev bind-mount of the source does not hide it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # matplotlib must not try to open a display inside the container.
    MPLBACKEND=Agg \
    PATH="/opt/venv/bin:$PATH"

# System libs some scientific wheels expect at runtime (OpenMP for numpy/scipy/cvxpy)
# plus curl for container healthchecks. build-essential covers any source builds.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) Install dependencies first (cached layer). Not --frozen: the lockfile is
#    regenerated when deps change; this stays robust during active E2 development.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project

# 2) Copy the project and install it. In dev the source is bind-mounted over /app,
#    so `uv run` still resolves the code from the mount at runtime.
COPY . .
RUN uv sync

EXPOSE 8000

# Default = API. The worker service overrides `command` in docker-compose.yml.
CMD ["uv", "run", "uvicorn", "saas.api:app", "--host", "0.0.0.0", "--port", "8000"]
