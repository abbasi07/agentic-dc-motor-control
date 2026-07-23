"""Rehydration helpers for design results that crossed a process boundary.

When a design run happens in the RQ worker (E2.3), the API process later serves the
job by rehydrating it from the DB. The live controller object is *not* serialized
(controllers hold numpy state / cvxpy problems); instead the durable JSON — the stored
``scorecard`` plus ``session_dict['best']`` — is enough to drive the certification gate
and export package, because ``agents.certify.export_certified_package`` only reads the
candidate's ``kind`` / ``params`` / ``scorecard`` and the controller's *name*.

Locked invariant: every number here already originated from a deterministic tool; we
only re-shape stored JSON, never recompute or invent metrics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np


def to_jsonable(obj: Any) -> Any:
    """Recursively coerce a value into JSON-native types for DB persistence.

    Scorecards carry numpy arrays/scalars (trajectory series, metrics) and can contain
    NaN/Inf. Those break ``json.dumps`` for Postgres JSONB, so we convert numpy ->
    python, arrays -> lists, and non-finite floats -> ``None`` (a metric that could not
    be measured). No number is altered otherwise — grounding is preserved.
    """
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, np.ndarray):
        return [to_jsonable(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(x) for x in obj]
    return str(obj)


@dataclass
class _NamedController:
    """Minimal stand-in exposing the ``.name`` the export writer reads."""

    name: str = "controller"


@dataclass
class RehydratedCandidate:
    """Candidate-shaped view over persisted JSON for the export/certify gate.

    Mirrors the attribute surface of ``agents.design_candidate.DesignCandidate`` that
    ``export_certified_package`` / ``certify_candidate`` touch: ``controller`` (name),
    ``kind``, ``params``, ``scorecard`` (and ``gains`` is intentionally absent so the
    certify path falls back to ``params``).
    """

    controller: _NamedController
    kind: str
    params: dict[str, Any]
    scorecard: dict[str, Any]
    gains: Any = None
    history: list[dict[str, Any]] = field(default_factory=list)


def rehydrated_candidate(job: Any) -> RehydratedCandidate | None:
    """Build an export-ready candidate stub from a job's persisted state.

    Returns ``None`` when there is nothing to export (no scorecard / best candidate).
    """
    scorecard = getattr(job, "scorecard", None)
    session_dict = getattr(job, "session_dict", None) or {}
    best = session_dict.get("best") or {}
    if not scorecard or not best:
        return None
    return RehydratedCandidate(
        controller=_NamedController(name=str(best.get("controller_name", "controller"))),
        kind=str(best.get("kind", "pid")),
        params=dict(best.get("params", {})),
        scorecard=scorecard,
    )


__all__ = ["RehydratedCandidate", "rehydrated_candidate", "to_jsonable"]
