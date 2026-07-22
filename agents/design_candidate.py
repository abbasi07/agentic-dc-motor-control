"""Generic scored controller candidate (PID and specialist topologies)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dc_motor.failure import FailureDigest, failure_digest_from_scorecard

from .pid_tuner import PIDGains, scorecard_objective


@dataclass
class DesignCandidate:
    """Topology-agnostic design result for the orchestrator / certification gate."""

    controller: Any
    kind: str  # pid | robust_pid | mpc | adaptive | ...
    params: dict[str, Any]
    scorecard: dict[str, Any]
    failure_digest: FailureDigest
    objective: float
    method: str = ""
    n_evaluations: int = 0
    notes: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def gains(self) -> PIDGains | None:
        """Back-compat for PID-like candidates (orchestrator sessions)."""
        if all(k in self.params for k in ("Kp", "Ki", "Kd")):
            return PIDGains(
                Kp=float(self.params["Kp"]),
                Ki=float(self.params["Ki"]),
                Kd=float(self.params["Kd"]),
            )
        return None

    def to_dict(self, *, include_scorecard_json: bool = False) -> dict[str, Any]:
        from dc_motor.evaluate import scorecard_to_json

        out: dict[str, Any] = {
            "kind": self.kind,
            "params": dict(self.params),
            "method": self.method,
            "n_evaluations": self.n_evaluations,
            "objective": self.objective,
            "all_pass": self.failure_digest.all_pass,
            "failure_digest": self.failure_digest.to_dict(),
            "mean_scalar_score": self.scorecard.get("summary", {}).get("mean_scalar_score"),
            "controller_name": getattr(self.controller, "name", self.controller.__class__.__name__),
            "notes": self.notes,
            "history": list(self.history),
        }
        if include_scorecard_json:
            out["scorecard_json"] = scorecard_to_json(self.scorecard)
        return out


def candidate_from_tune_result(result) -> DesignCandidate:
    """Wrap a TuneResult into DesignCandidate."""
    return DesignCandidate(
        controller=result.controller,
        kind="pid",
        params=result.gains.to_dict(),
        scorecard=result.scorecard,
        failure_digest=result.failure_digest,
        objective=result.objective,
        method=result.method,
        n_evaluations=result.n_evaluations,
        notes=result.notes,
        history=list(result.history),
    )


def candidate_from_controller(
    controller: Any,
    scorecard: dict[str, Any],
    *,
    kind: str,
    params: dict[str, Any],
    method: str = "",
    n_evaluations: int = 1,
    notes: str = "",
) -> DesignCandidate:
    digest = failure_digest_from_scorecard(scorecard)
    return DesignCandidate(
        controller=controller,
        kind=kind,
        params=params,
        scorecard=scorecard,
        failure_digest=digest,
        objective=scorecard_objective(scorecard),
        method=method,
        n_evaluations=n_evaluations,
        notes=notes,
    )
