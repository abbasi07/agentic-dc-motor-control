"""Shared helpers for runnable lab examples."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_project_on_path() -> None:
    """Allow `uv run python examples/...` without an editable install."""
    root = str(project_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def add_plot_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Show matplotlib figures (default: print-only).",
    )


def require_openai_or_exit() -> None:
    """Exit non-zero with a clear message if OpenAI is unavailable."""
    load_dotenv(project_root() / ".env")
    ensure_project_on_path()
    from agents.spec_agent import llm_unavailable_message

    key = os.getenv("OPENAI_API_KEY")
    if not key or key.startswith("sk-your-key"):
        print(llm_unavailable_message(detail="OPENAI_API_KEY missing or placeholder."))
        raise SystemExit(1)


def maybe_show(plot: bool) -> None:
    if not plot:
        import matplotlib

        matplotlib.use("Agg")
