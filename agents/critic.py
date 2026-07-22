"""Grounded design critic: scorecard/FailureDigest -> next-action recommendation.

The critic NEVER invents numbers. It reads only the deterministic
:class:`~dc_motor.failure.FailureDigest` (already derived from the scorecard) and
the controller registry's tag→family map, and produces a structured, ordered
recommendation the orchestrator (or LLM) can act on. This sharpens the
diagnose→redesign policy without adding an external framework.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from dc_motor.failure import FailureDigest
from dc_motor.specs import DesignSpec

from .controller_registry import families_for_tags, get_family_by_kind
from .design_candidate import DesignCandidate


@dataclass
class Diagnosis:
    """Structured, grounded recommendation for the next redesign action."""

    all_pass: bool
    current_kind: str
    tags: list[str] = field(default_factory=list)
    failed_scenarios: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    recommended_families: list[str] = field(default_factory=list)
    grounded_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def diagnose(
    candidate: DesignCandidate,
    spec: DesignSpec,
    *,
    tried_actions: tuple[str, ...] | list[str] = (),
) -> Diagnosis:
    """Build a grounded diagnosis from a scored candidate.

    ``recommended_actions`` is ordered: documented tag→action hints first, then
    controller-family actions whose strengths overlap the failure tags, minus any
    actions already tried. Everything is derived from the FailureDigest — no
    numbers are fabricated.
    """
    digest: FailureDigest = candidate.failure_digest
    tried = set(tried_actions)

    actions: list[str] = []
    for hint in digest.action_hints:
        if hint not in actions:
            actions.append(hint)

    families = families_for_tags(digest.tags)
    # Skip recommending the family we already have as "best".
    current = get_family_by_kind(candidate.kind)
    family_actions: list[str] = []
    family_names: list[str] = []
    for fam in families:
        if current is not None and fam.kind == current.kind:
            continue
        family_names.append(fam.type_name)
        if fam.action not in actions and fam.action not in family_actions:
            family_actions.append(fam.action)

    for a in family_actions:
        if a not in actions:
            actions.append(a)

    # Drop already-tried actions but keep at least one suggestion if possible.
    pruned = [a for a in actions if a not in tried]
    ordered = pruned or actions

    return Diagnosis(
        all_pass=digest.all_pass,
        current_kind=candidate.kind,
        tags=list(digest.tags),
        failed_scenarios=list(digest.failed_scenarios),
        recommended_actions=ordered,
        recommended_families=family_names,
        grounded_summary=digest.summary,
    )


__all__ = ["Diagnosis", "diagnose"]
