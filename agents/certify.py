"""Simulation certification gate + export package (no hardware)."""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dc_motor.evaluate import scorecard_to_json
from dc_motor.failure import failure_digest_from_scorecard


@dataclass
class CertificationResult:
    """Hard gate: certified iff all hard constraints pass on required scenarios."""

    allowed: bool
    reason: str
    controller_name: str = ""
    kind: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    scorecard_summary: dict[str, Any] = field(default_factory=dict)
    failure_digest: dict[str, Any] = field(default_factory=dict)
    blocked_failures: list[dict[str, Any]] = field(default_factory=list)
    timestamp_utc: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def certify_scorecard(
    scorecard: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    kind: str = "",
    require_pass_rate: float = 1.0,
) -> CertificationResult:
    """Code-enforced certification gate. LLM may explain, never override."""
    digest = failure_digest_from_scorecard(scorecard)
    summary = scorecard.get("summary", {})
    pass_rate = float(summary.get("pass_rate", 1.0 if digest.all_pass else 0.0))
    allowed = bool(digest.all_pass) and pass_rate >= require_pass_rate
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if allowed:
        reason = (
            f"ALLOW: all hard constraints passed "
            f"({summary.get('n_scenarios_pass', '?')}/{summary.get('n_scenarios', '?')} scenarios); "
            f"mean_scalar_score={summary.get('mean_scalar_score')}; "
            f"worst_ITAE={summary.get('worst_case_ITAE')}."
        )
    else:
        reason = (
            f"BLOCK: certification refused. {digest.summary} "
            "Export of a 'certified' package is not permitted."
        )
    return CertificationResult(
        allowed=allowed,
        reason=reason,
        controller_name=str(scorecard.get("controller", "")),
        kind=kind,
        params=dict(params or {}),
        scorecard_summary=dict(summary),
        failure_digest=digest.to_dict(),
        blocked_failures=[f.to_dict() for f in digest.failures],
        timestamp_utc=ts,
    )


def certify_candidate(candidate) -> CertificationResult:
    """Certify a DesignCandidate / TuneResult-like object."""
    scorecard = candidate.scorecard
    params = getattr(candidate, "params", None)
    if params is None and getattr(candidate, "gains", None) is not None:
        g = candidate.gains
        params = g.to_dict() if hasattr(g, "to_dict") else dict(g)
    kind = getattr(candidate, "kind", "pid")
    return certify_scorecard(scorecard, params=params or {}, kind=kind)


def export_certified_package(
    candidate,
    *,
    rationale: str,
    out_dir: str | Path,
    nl_spec: str = "",
    action_trace: list[dict[str, Any]] | None = None,
    package_name: str | None = None,
) -> Path:
    """Write controller JSON + scorecard + rationale (+ optional zip).

    Raises PermissionError if certification gate blocks.
    """
    cert = certify_candidate(candidate)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = package_name or f"controller_package_{stamp}"
    pkg = out / name
    pkg.mkdir(parents=True, exist_ok=True)

    controller_cfg = {
        "name": getattr(candidate.controller, "name", candidate.controller.__class__.__name__),
        "kind": getattr(candidate, "kind", "pid"),
        "params": getattr(candidate, "params", {}),
        "interface": {"reset": "reset()", "step": "step(measurement, reference, dt) -> u"},
        "simulation_only": True,
        "certified": cert.allowed,
    }
    (pkg / "controller.json").write_text(json.dumps(controller_cfg, indent=2), encoding="utf-8")
    (pkg / "scorecard.json").write_text(scorecard_to_json(candidate.scorecard), encoding="utf-8")
    (pkg / "certification.json").write_text(cert.to_json(), encoding="utf-8")
    (pkg / "rationale.md").write_text(rationale.strip() + "\n", encoding="utf-8")
    meta = {
        "nl_spec": nl_spec,
        "action_trace": action_trace or [],
        "exported_at_utc": cert.timestamp_utc,
        "scope": "simulation_certification_only",
    }
    (pkg / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if not cert.allowed:
        (pkg / "BLOCKED.txt").write_text(cert.reason + "\n", encoding="utf-8")
        # Still write diagnostics, but do not produce certified.zip
        return pkg

    zip_path = out / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in pkg.iterdir():
            zf.write(p, arcname=f"{name}/{p.name}")
    return zip_path


__all__ = [
    "CertificationResult",
    "certify_scorecard",
    "certify_candidate",
    "export_certified_package",
]
