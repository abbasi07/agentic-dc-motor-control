"""Formal DesignSpec: NL specs become validated constraints for orchestration."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

# Practical ceiling for reference speed (≈ 9550 RPM). Real motors are often much
# slower; feasibility checks against plant ω_max catch unreachable targets.
_OMEGA_REF_MAX_RAD_S = 1000.0
_RPM_TO_RAD_S = 2.0 * math.pi / 60.0

# Metrics produced by dc_motor.metrics / evaluate
ALLOWED_METRICS = frozenset(
    {
        "rise_time_s",
        "settling_time_s",
        "overshoot_pct",
        "steady_state_error",
        "IAE",
        "ISE",
        "ITAE",
        "control_effort",
        "saturation_time_s",
        "recovery_time_s",
    }
)

ALLOWED_OPS = frozenset({"<=", ">=", "<", ">"})

ALLOWED_SCENARIOS = frozenset(
    {
        "step_1rads",
        "load_disturbance",
        "plant_mismatch",
        "noisy_measurement",
        "mismatch_load",
        "noise_low",
        "noise_med",
        "noise_high",
        "mismatch_harsh",
    }
)

# Absolute simulation-horizon ceiling and the largest time-based metric limit we
# accept. These are generous on purpose: slow motors (τ_mech in the tens of
# seconds) legitimately need settling times / horizons far beyond the old 15 s cap.
MAX_HORIZON_S = 3600.0
MAX_TIME_METRIC_S = 600.0

# Safety clamps: (min, max) inclusive for numeric DesignSpec fields / limits.
# Bounds only guard against absurd / non-physical values; they must NOT silently
# rewrite realistic engineering targets (any clamp is surfaced as a warning).
METRIC_LIMIT_BOUNDS: dict[str, tuple[float, float]] = {
    "rise_time_s": (0.001, MAX_TIME_METRIC_S),
    "settling_time_s": (0.01, MAX_TIME_METRIC_S),
    "overshoot_pct": (0.0, 100.0),
    "steady_state_error": (1e-6, 1.0),
    "IAE": (0.0, 1e7),
    "ISE": (0.0, 1e7),
    "ITAE": (0.0, 1e7),
    "control_effort": (0.0, 1e5),
    "saturation_time_s": (0.0, MAX_TIME_METRIC_S),
    "recovery_time_s": (0.0, MAX_TIME_METRIC_S),
}

# Metrics that are almost always intended when a user describes speed control.
CORE_METRICS: tuple[str, ...] = (
    "settling_time_s",
    "overshoot_pct",
    "steady_state_error",
)

# Provenance tags: WHERE a spec value came from. This is the backbone of the
# "the agent must disclose anything it supplied" guarantee — the LLM leads
# interpretation, but every value is labelled so nothing is silently injected.
PROV_USER = "user"        # stated explicitly by the engineer (verified in their text)
PROV_LLM = "llm"          # inferred / chosen by the interpreter (must be disclosed)
PROV_DEFAULT = "default"  # safe engine default (must be disclosed)
PROV_DERIVED = "derived"  # deterministically derived from another value (e.g. horizon)
PROV_CLAMPED = "clamped"  # a stated value was adjusted to a safety bound (must be disclosed)

# Natural-language triggers that justify a non-nominal scenario. A scenario is
# only allowed into a spec when the user's own words support it (or it is added
# later by an explicit modify/expand action) — never because an LLM copied it
# from a schema example.
SCENARIO_TRIGGERS: dict[str, tuple[str, ...]] = {
    "load_disturbance": ("load", "disturbance", "torque"),
    "plant_mismatch": ("mismatch", "uncertain", "parameter variation", "parameter change"),
    "noisy_measurement": ("noise", "noisy", "sensor"),
}
# Phrases that negate a load requirement so "no load" does not add the scenario.
_LOAD_NEGATIONS = ("no load", "without load", "no disturbance", "without disturbance")


@dataclass
class DesignSpec:
    """Validated design requirements produced by Spec Interpreter (LLM or template)."""

    raw_spec: str
    hard_constraints: dict[str, tuple[str, float]] = field(default_factory=dict)
    soft_preferences: dict[str, float] = field(default_factory=dict)
    required_scenarios: list[str] = field(default_factory=list)
    omega_ref: float = 1.0
    V_min: float = -12.0
    V_max: float = 12.0
    t_final: float = 3.0
    max_design_iterations: int = 5
    stop_on_pass: bool = True
    source: str = "manual"  # manual | template | llm
    notes: str = ""
    warnings: list[str] = field(default_factory=list)
    # Per-value source tags, e.g. {"settling_time_s": "user", "t_final": "derived",
    # "omega_ref": "user", "scenario:load_disturbance": "llm"}. Empty == unknown
    # (treated as user-provided for legacy/manual specs).
    provenance: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # JSON-friendly constraints: {"metric": {"op": "<=", "limit": 1.2}}
        d["hard_constraints"] = {
            k: {"op": op, "limit": lim} for k, (op, lim) in self.hard_constraints.items()
        }
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def constraints_for_evaluator(self) -> dict[str, tuple[str, float]]:
        """Shape expected by evaluate_controller(..., constraints=...)."""
        return dict(self.hard_constraints)

    def score_weights_for_evaluator(self) -> dict[str, float]:
        if self.soft_preferences:
            return dict(self.soft_preferences)
        return {
            "ITAE": 1.0,
            "overshoot_pct": 0.05,
            "control_effort": 0.01,
            "saturation_time_s": 0.1,
        }


class DesignSpecValidationError(ValueError):
    """Raised when a DesignSpec cannot be made safe/valid."""


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool]:
    clamped = min(hi, max(lo, float(value)))
    return clamped, clamped != float(value)


def rpm_to_rad_s(rpm: float) -> float:
    """Convert revolutions per minute to rad/s."""
    return float(rpm) * _RPM_TO_RAD_S


def extract_omega_ref_from_text(text: str) -> float | None:
    """Deterministically parse a target speed from NL (RPM preferred, else rad/s).

    Returns None when no explicit speed is found. RPM always wins when present so
    LLM defaults (ω_ref=1) cannot silently discard a stated target.
    """
    if not text or not str(text).strip():
        return None
    raw = str(text)

    rpm = re.search(
        r"(?P<v>\d+(?:\.\d+)?)\s*(?:rpm|r\.?\s*p\.?\s*m\.?|"
        r"rev(?:olutions?)?\s*(?:per|/)\s*min(?:ute)?s?)",
        raw,
        re.IGNORECASE,
    )
    if rpm:
        return rpm_to_rad_s(float(rpm.group("v")))

    rad = re.search(
        r"(?:omega_ref|ω_ref|ω\s*=|target\s+speed|reference\s+speed|speed|"
        r"reference)\s*(?:=|to|:)?\s*(?P<v>\d+(?:\.\d+)?)\s*"
        r"(?:rad(?:ians?)?\s*/\s*s(?:ec(?:ond)?s?)?|rad/s)",
        raw,
        re.IGNORECASE,
    )
    if rad:
        return float(rad.group("v"))

    # Bare "omega_ref = 50" without unit (legacy template style).
    bare = re.search(
        r"(?:omega_ref|ω_ref)\s*(?:=|to|:)\s*(?P<v>\d+(?:\.\d+)?)",
        raw,
        re.IGNORECASE,
    )
    if bare:
        return float(bare.group("v"))
    return None


# --------------------------------------------------------------------------- #
# Deterministic requirement extraction from the user's own words.
# These functions are the backbone of "the LLM may not invent numbers": every
# numeric target and scenario the engine acts on is grounded in the raw text.
# --------------------------------------------------------------------------- #

_NUM = r"(?P<v>\d+(?:\.\d+)?)"
# Comparator / connective words that all mean "the following number is the limit".
_LE = (
    r"(?:<=|<|=|:|to|of|under|below|within|be|at\s+most|no\s+more\s+than|"
    r"max(?:imum)?|less\s+than|tol(?:erance)?)"
)
_SEC = r"(?:s|sec|secs|second|seconds)"

_SETTLING_TEXT_RE = re.compile(
    rf"settl(?:e|ing)(?:\s*time)?\s*(?:{_LE}\s*)*{_NUM}\s*{_SEC}\b", re.IGNORECASE
)
_RISE_TEXT_RE = re.compile(
    rf"rise(?:\s*time)?\s*(?:{_LE}\s*)*{_NUM}\s*{_SEC}\b", re.IGNORECASE
)
_OVERSHOOT_TEXT_RE = re.compile(
    rf"(?:max(?:imum)?\s+)?overshoot\s*(?:{_LE}\s*)*{_NUM}\s*%?", re.IGNORECASE
)
_SSE_TEXT_RE = re.compile(
    r"(?:steady[-\s]*state\s*error|ss[-\s]*error|steady[-\s]*state)\s*"
    rf"(?:tol(?:erance)?)?\s*(?:{_LE}\s*)*{_NUM}\s*(?P<pct>%)?",
    re.IGNORECASE,
)
_HORIZON_TEXT_RE = re.compile(
    r"(?:simulation\s+horizon|sim(?:ulation)?\s+time|time\s+horizon|horizon|"
    r"t_final|t\s*final|simulate\s+(?:for|to)|run\s+(?:for|to))\s*"
    rf"(?:(?:{_LE}|at\s+least|>=)\s*)*{_NUM}\s*{_SEC}?\b",
    re.IGNORECASE,
)


def extract_constraints_from_text(text: str) -> dict[str, tuple[str, float]]:
    """Deterministically pull hard-constraint limits from natural language.

    Returns only what is explicitly stated (op always normalised to ``<=`` for
    these upper-bound targets). Percent-style values for steady-state error are
    converted to a fraction (``5%`` -> ``0.05``).
    """
    if not text:
        return {}
    out: dict[str, tuple[str, float]] = {}

    m = _SETTLING_TEXT_RE.search(text)
    if m:
        out["settling_time_s"] = ("<=", float(m.group("v")))

    m = _RISE_TEXT_RE.search(text)
    if m:
        out["rise_time_s"] = ("<=", float(m.group("v")))

    m = _OVERSHOOT_TEXT_RE.search(text)
    if m:
        out["overshoot_pct"] = ("<=", float(m.group("v")))

    m = _SSE_TEXT_RE.search(text)
    if m:
        v = float(m.group("v"))
        if m.group("pct") or v > 1.0:
            v = v / 100.0
        out["steady_state_error"] = ("<=", v)

    return out


def extract_t_final_from_text(text: str) -> float | None:
    """Deterministically pull an explicit simulation horizon from the text."""
    if not text:
        return None
    m = _HORIZON_TEXT_RE.search(text)
    if m:
        return float(m.group("v"))
    return None


def suggest_t_final(
    *,
    settling: float | None = None,
    explicit: float | None = None,
    current: float | None = None,
) -> float:
    """Choose a horizon deterministically.

    Precedence: an explicit user horizon wins; otherwise derive a horizon that
    comfortably covers the settling target (1.5x, rounded up) without ever
    shrinking a horizon the user already has.
    """
    if explicit is not None and explicit > 0:
        return min(MAX_HORIZON_S, float(explicit))
    candidates = [float(current)] if current else []
    if settling is not None and settling > 0:
        candidates.append(float(math.ceil(settling * 1.5)))
    if not candidates:
        return 3.0
    return min(MAX_HORIZON_S, max(candidates))


def scenarios_from_text(text: str) -> list[str]:
    """Scenarios justified by the user's words (nominal step always included)."""
    result = ["step_1rads"]
    if not text:
        return result
    tl = text.lower()
    for scenario, triggers in SCENARIO_TRIGGERS.items():
        if scenario == "load_disturbance" and any(neg in tl for neg in _LOAD_NEGATIONS):
            continue
        if any(trigger in tl for trigger in triggers):
            if scenario not in result:
                result.append(scenario)
    return result


def reconcile_scenarios_with_text(scenarios: list[str], text: str) -> list[str]:
    """Keep only scenarios the user's text supports; add any it implies.

    step_1rads is always kept. This is the single guard that stops a weak LLM from
    smuggling scenarios (e.g. load_disturbance copied from a schema example) into a
    spec the user never asked for.
    """
    supported = set(scenarios_from_text(text))
    result: list[str] = []
    for name in scenarios:
        if name == "step_1rads" or name in supported:
            if name not in result:
                result.append(name)
    for name in scenarios_from_text(text):
        if name not in result:
            result.append(name)
    if "step_1rads" not in result:
        result.insert(0, "step_1rads")
    return result


def apply_text_derived_targets(spec: DesignSpec) -> DesignSpec:
    """Overlay deterministic NL extractions (esp. RPM→rad/s) onto a DesignSpec."""
    warnings = list(spec.warnings)
    omega = float(spec.omega_ref)
    extracted = extract_omega_ref_from_text(spec.raw_spec)
    if extracted is not None:
        has_rpm = bool(
            re.search(r"rpm|r\.?\s*p\.?\s*m\.?|rev(?:olutions?)?\s*(?:per|/)", spec.raw_spec, re.I)
        )
        # Prefer text when RPM was stated, or when the LLM left the schema default.
        if has_rpm or abs(omega - 1.0) < 1e-9:
            if abs(omega - extracted) > 1e-3:
                unit_note = " (from RPM)" if has_rpm else ""
                warnings.append(
                    f"Set omega_ref from requirements text{unit_note}: {extracted:.4g} rad/s"
                )
            omega = extracted
            provenance = dict(spec.provenance)
            provenance["omega_ref"] = PROV_USER  # grounded in the user's own text
            return replace(spec, omega_ref=omega, warnings=warnings, provenance=provenance)

    return replace(spec, omega_ref=omega, warnings=warnings)


def apply_plant_voltage_budget(spec: DesignSpec, V_max: float) -> DesignSpec:
    """Align Operating Point voltage limits with the confirmed plant actuator budget."""
    v = abs(float(V_max))
    if v <= 0:
        return spec
    warnings = list(spec.warnings)
    provenance = dict(spec.provenance)
    if abs(float(spec.V_max) - v) > 1e-9 or abs(float(spec.V_min) + v) > 1e-9:
        warnings.append(
            f"Inherited plant voltage budget ±{v:g} V into the operating point "
            f"(was {spec.V_min:g}…{spec.V_max:g} V)."
        )
        provenance["V_max"] = PROV_DERIVED  # from the confirmed plant, not the user
        provenance["V_min"] = PROV_DERIVED
    return replace(spec, V_min=-v, V_max=v, warnings=warnings, provenance=provenance)


def reconcile_spec_with_plant(
    spec: DesignSpec,
    *,
    plant_V_max: float | None = None,
) -> DesignSpec:
    """Post-process a DesignSpec so Operating Point matches plant + NL text."""
    updated = apply_text_derived_targets(spec)
    if plant_V_max is not None:
        updated = apply_plant_voltage_budget(updated, plant_V_max)
    return validate_and_clamp_design_spec(updated)


def validate_and_clamp_design_spec(spec: DesignSpec) -> DesignSpec:
    """Validate schema and clamp absurd limits. Mutates a copy-safe new warnings list."""
    warnings: list[str] = list(spec.warnings)
    provenance: dict[str, str] = dict(spec.provenance)

    # Constraints
    cleaned: dict[str, tuple[str, float]] = {}
    for metric, pair in spec.hard_constraints.items():
        if isinstance(pair, dict):
            op, limit = pair["op"], float(pair["limit"])
        else:
            op, limit = pair[0], float(pair[1])

        if metric not in ALLOWED_METRICS:
            raise DesignSpecValidationError(f"Unknown metric: {metric}")
        if op not in ALLOWED_OPS:
            raise DesignSpecValidationError(f"Unsupported op for {metric}: {op}")

        lo, hi = METRIC_LIMIT_BOUNDS[metric]
        limit, changed = _clamp(limit, lo, hi)
        if changed:
            warnings.append(
                f"Adjusted {metric} to the allowed range [{lo:g}, {hi:g}] -> {limit:g} "
                "(safety bound)."
            )
            provenance[metric] = PROV_CLAMPED
        cleaned[metric] = (op, limit)

    if not cleaned:
        # Sensible defaults if NL parse yielded nothing
        cleaned = {
            "settling_time_s": ("<=", 2.0),
            "overshoot_pct": ("<=", 15.0),
            "steady_state_error": ("<=", 0.05),
        }
        warnings.append("No hard constraints found; applied default evaluation constraints.")
        for metric in cleaned:
            provenance[metric] = PROV_DEFAULT

    # Scenarios
    scenarios = []
    for name in spec.required_scenarios:
        if name not in ALLOWED_SCENARIOS:
            warnings.append(f"Dropped unknown scenario: {name}")
            continue
        if name not in scenarios:
            scenarios.append(name)
    if not scenarios:
        scenarios = ["step_1rads"]
        warnings.append("No valid scenarios; defaulted to ['step_1rads'].")

    # Load disturbance is applied at t=1.5 s in the catalog; a global settling
    # limit below that onset cannot be met on load_disturbance (metric is absolute time).
    _LOAD_ONSET_S = 1.5
    if "load_disturbance" in scenarios:
        settle = cleaned.get("settling_time_s")
        if settle is not None:
            op, lim = settle
            if op in {"<=", "<"} and lim < _LOAD_ONSET_S:
                warnings.append(
                    f"settling_time_s {op} {lim} with load_disturbance is likely "
                    f"infeasible (load onset at t={_LOAD_ONSET_S}s; settling is absolute). "
                    "Prefer settling >= 2.0s when requiring load tests, or drop settling "
                    "from hard constraints on that scenario in a later redesign."
                )

    # Soft preferences (lower-is-better weights)
    prefs: dict[str, float] = {}
    for metric, weight in spec.soft_preferences.items():
        if metric not in ALLOWED_METRICS:
            warnings.append(f"Dropped unknown soft preference metric: {metric}")
            continue
        w = float(weight)
        if w < 0:
            warnings.append(f"Soft weight for {metric} was negative; set to 0.")
            w = 0.0
        prefs[metric] = w

    omega_ref, ch = _clamp(spec.omega_ref, 0.01, _OMEGA_REF_MAX_RAD_S)
    if ch:
        warnings.append(f"Clamped omega_ref -> {omega_ref}")

    V_max, ch = _clamp(abs(spec.V_max), 1.0, 48.0)
    if ch:
        warnings.append(f"Clamped |V_max| -> {V_max}")
    V_min = -V_max if spec.V_min < 0 else float(spec.V_min)
    V_min, ch2 = _clamp(V_min, -48.0, -1.0)
    if ch2:
        warnings.append(f"Clamped V_min -> {V_min}")

    t_final, ch = _clamp(spec.t_final, 0.1, MAX_HORIZON_S)
    if ch:
        warnings.append(f"Adjusted t_final to the allowed range -> {t_final:g} s (safety bound).")
        provenance["t_final"] = PROV_CLAMPED

    # A horizon shorter than the settling target can never reveal settling; nudge it
    # up deterministically (never silently below what the user asked to observe).
    settle = cleaned.get("settling_time_s")
    if settle is not None and settle[0] in {"<=", "<"}:
        needed = min(MAX_HORIZON_S, math.ceil(float(settle[1]) * 1.2))
        if t_final < needed:
            warnings.append(
                f"Raised t_final {t_final:g} -> {needed:g} s so the {settle[1]:g}s "
                "settling target is observable within the simulation horizon."
            )
            t_final = float(needed)
            provenance["t_final"] = PROV_DERIVED

    max_iters = int(spec.max_design_iterations)
    if max_iters < 1:
        max_iters = 1
        warnings.append("max_design_iterations raised to 1")
    if max_iters > 20:
        max_iters = 20
        warnings.append("max_design_iterations capped at 20")

    return replace(
        spec,
        hard_constraints=cleaned,
        soft_preferences=prefs,
        required_scenarios=scenarios,
        omega_ref=omega_ref,
        V_min=V_min,
        V_max=V_max,
        t_final=t_final,
        max_design_iterations=max_iters,
        stop_on_pass=bool(spec.stop_on_pass),
        warnings=warnings,
        provenance=provenance,
    )


def design_spec_from_dict(data: dict[str, Any], raw_spec: str = "", source: str = "manual") -> DesignSpec:
    """Build DesignSpec from LLM/JSON dict, then validate/clamp."""
    raw = data.get("raw_spec", raw_spec) or raw_spec
    hc_in = data.get("hard_constraints", {})
    hard: dict[str, tuple[str, float]] = {}
    for metric, body in hc_in.items():
        if isinstance(body, dict):
            hard[metric] = (str(body["op"]), float(body["limit"]))
        elif isinstance(body, (list, tuple)) and len(body) == 2:
            hard[metric] = (str(body[0]), float(body[1]))
        else:
            raise DesignSpecValidationError(f"Bad constraint entry for {metric}: {body}")

    prefs = {k: float(v) for k, v in data.get("soft_preferences", {}).items()}
    scenarios = list(data.get("required_scenarios", []))

    draft = DesignSpec(
        raw_spec=raw,
        hard_constraints=hard,
        soft_preferences=prefs,
        required_scenarios=scenarios,
        omega_ref=float(data.get("omega_ref", 1.0)),
        V_min=float(data.get("V_min", -12.0)),
        V_max=float(data.get("V_max", 12.0)),
        t_final=float(data.get("t_final", 3.0)),
        max_design_iterations=int(data.get("max_design_iterations", 5)),
        stop_on_pass=bool(data.get("stop_on_pass", True)),
        source=source,
        notes=str(data.get("notes", "")),
        warnings=list(data.get("warnings", [])),
        provenance=dict(data.get("provenance", {})),
    )
    # Deterministic NL overlay (RPM→rad/s) before safety clamps.
    return validate_and_clamp_design_spec(apply_text_derived_targets(draft))


def finalize_llm_spec(spec: DesignSpec, text: str) -> DesignSpec:
    """Label an interpreter-produced spec with provenance and reconcile with the text.

    Philosophy: the LLM LEADS interpretation; the deterministic layer VERIFIES and
    labels, it does not silently override the model's reasoning. Specifically:

    * A number the engineer stated in ``text`` is authoritative — if the interpreter
      contradicts it, we correct to the text value and record the correction
      (provenance = ``user``).
    * A value the interpreter supplied that the engineer did NOT state is KEPT
      (so the agent stays intelligent) but tagged ``llm`` so it MUST be disclosed.
    * Scenarios the text supports are active (``user``); scenarios the interpreter
      merely suggested are held back as suggestions the agent can offer — never
      silently activated.
    * The horizon is taken from the text (``user``) or derived from settling
      (``derived``).
    """
    warnings = list(spec.warnings)
    provenance = dict(spec.provenance)
    text_constraints = extract_constraints_from_text(text)

    merged: dict[str, tuple[str, float]] = {}
    for metric, pair in spec.hard_constraints.items():
        if metric in text_constraints:
            t_pair = text_constraints[metric]
            if abs(float(t_pair[1]) - float(pair[1])) > 1e-9 or t_pair[0] != pair[0]:
                warnings.append(
                    f"Corrected {metric} to your stated value {t_pair[0]} {t_pair[1]:g} "
                    f"(interpreter had {pair[0]} {pair[1]:g})."
                )
            merged[metric] = t_pair
            provenance[metric] = PROV_USER
        else:
            # Keep the interpreter's value (agentic) but flag it for disclosure.
            merged[metric] = pair
            provenance[metric] = PROV_LLM
    for metric, pair in text_constraints.items():
        if metric not in merged:
            merged[metric] = pair
            provenance[metric] = PROV_USER

    # Active scenarios = what the text supports. Interpreter-only scenarios become
    # suggestions the agent can offer, never silent additions.
    active = reconcile_scenarios_with_text([], text)  # user-grounded set (+ step)
    for name in active:
        provenance[f"scenario:{name}"] = PROV_USER
    for name in spec.required_scenarios:
        if name not in active and name in ALLOWED_SCENARIOS and name != "step_1rads":
            provenance[f"scenario_suggested:{name}"] = PROV_LLM
            warnings.append(
                f"Did not auto-add scenario '{name}' (you did not request it); "
                "offer it to the engineer if it is relevant."
            )

    # Horizon: explicit user value wins, else derived from settling.
    settle = merged.get("settling_time_s")
    settle_v = float(settle[1]) if settle and settle[0] in {"<=", "<"} else None
    explicit_tf = extract_t_final_from_text(text)
    t_final = suggest_t_final(settling=settle_v, explicit=explicit_tf, current=spec.t_final)
    provenance["t_final"] = PROV_USER if explicit_tf is not None else PROV_DERIVED

    # omega_ref: user (in text) handled by apply_text_derived_targets below; here we
    # pre-tag the interpreter/default case so nothing is left unlabeled.
    if extract_omega_ref_from_text(text) is None:
        provenance["omega_ref"] = (
            PROV_DEFAULT if abs(float(spec.omega_ref) - 1.0) < 1e-9 else PROV_LLM
        )

    grounded = replace(
        spec,
        hard_constraints=merged,
        required_scenarios=list(active),
        t_final=t_final,
        warnings=warnings,
        provenance=provenance,
    )
    return validate_and_clamp_design_spec(apply_text_derived_targets(grounded))


def apply_spec_edits(
    spec: DesignSpec,
    *,
    settling: float | None = None,
    overshoot: float | None = None,
    steady_state_error: float | None = None,
    rise_time: float | None = None,
    t_final: float | None = None,
    add_scenarios: list[str] | None = None,
    remove_scenarios: list[str] | None = None,
) -> tuple[DesignSpec, list[str]]:
    """Apply explicit, deterministic edits to a spec (used by the modify path).

    Returns the validated spec and a human-readable list of the edits actually
    applied, so the agent can report exactly what changed (never invented).
    """
    hard = dict(spec.hard_constraints)
    scenarios = list(spec.required_scenarios)
    provenance = dict(spec.provenance)
    changes: list[str] = []

    def _set(metric: str, value: float | None, fmt: str) -> None:
        if value is None:
            return
        op = hard[metric][0] if metric in hard and hard[metric][0] in {"<=", "<"} else "<="
        hard[metric] = (op, float(value))
        provenance[metric] = PROV_USER  # an explicit edit is a user decision
        changes.append(fmt.format(op=op, v=float(value)))

    _set("settling_time_s", settling, "settling_time_s {op} {v:g} s")
    _set("overshoot_pct", overshoot, "overshoot_pct {op} {v:g} %")
    _set("steady_state_error", steady_state_error, "steady_state_error {op} {v:g}")
    _set("rise_time_s", rise_time, "rise_time_s {op} {v:g} s")

    for name in add_scenarios or []:
        if name in ALLOWED_SCENARIOS and name not in scenarios:
            scenarios.append(name)
            provenance[f"scenario:{name}"] = PROV_USER
            changes.append(f"added scenario '{name}'")
    for name in remove_scenarios or []:
        if name in scenarios and name != "step_1rads":
            scenarios.remove(name)
            provenance.pop(f"scenario:{name}", None)
            changes.append(f"removed scenario '{name}'")

    # Horizon: explicit wins; otherwise track a changed settling target.
    new_settle = hard.get("settling_time_s")
    settle_v = float(new_settle[1]) if new_settle and new_settle[0] in {"<=", "<"} else None
    resolved_tf = suggest_t_final(
        settling=settle_v if settling is not None else None,
        explicit=t_final,
        current=spec.t_final,
    )
    if abs(resolved_tf - float(spec.t_final)) > 1e-9:
        provenance["t_final"] = PROV_USER if t_final is not None else PROV_DERIVED
        changes.append(f"t_final -> {resolved_tf:g} s")

    edited = replace(
        spec,
        hard_constraints=hard,
        required_scenarios=scenarios,
        t_final=resolved_tf,
        provenance=provenance,
    )
    return validate_and_clamp_design_spec(edited), changes


_METRIC_HUMAN = {
    "settling_time_s": ("settling time", "s"),
    "rise_time_s": ("rise time", "s"),
    "overshoot_pct": ("overshoot", "%"),
    "steady_state_error": ("steady-state error", ""),
    "control_effort": ("control effort", ""),
    "saturation_time_s": ("saturation time", "s"),
    "recovery_time_s": ("recovery time", "s"),
    "IAE": ("IAE", ""),
    "ISE": ("ISE", ""),
    "ITAE": ("ITAE", ""),
}


def _fmt_metric(metric: str, op: str, lim: float) -> str:
    label, unit = _METRIC_HUMAN.get(metric, (metric, ""))
    suffix = f" {unit}" if unit and unit != "%" else (unit or "")
    return f"{label} {op} {lim:g}{suffix}"


def build_disclosures(spec: DesignSpec) -> list[str]:
    """Human-readable notes the agent MUST relay: every value it did not get from
    the engineer (assumed/derived/defaulted/clamped) and any held-back suggestions.

    This is what turns provenance into a hard "the agent discloses what it supplied"
    behaviour rather than relying on the model to volunteer it.
    """
    prov = spec.provenance or {}
    msgs: list[str] = []

    for metric, (op, lim) in spec.hard_constraints.items():
        src = prov.get(metric)
        desc = _fmt_metric(metric, op, lim)
        if src == PROV_LLM:
            msgs.append(f"ASSUMED (you did not specify): {desc}. Confirm or change it.")
        elif src == PROV_DEFAULT:
            msgs.append(f"DEFAULT applied (you did not specify): {desc}. Confirm or change it.")
        elif src == PROV_CLAMPED:
            msgs.append(f"ADJUSTED to a safety bound: {desc}. Verify this still meets your intent.")

    if prov.get("omega_ref") == PROV_LLM:
        msgs.append(
            f"ASSUMED target speed omega_ref = {spec.omega_ref:g} rad/s (you did not "
            "state one). Confirm the target speed."
        )
    elif prov.get("omega_ref") == PROV_DEFAULT:
        msgs.append(
            "MISSING: no target speed was given; using the placeholder "
            f"{spec.omega_ref:g} rad/s. Please specify the target speed."
        )

    tf_src = prov.get("t_final")
    if tf_src == PROV_DERIVED:
        msgs.append(
            f"DERIVED simulation horizon t_final = {spec.t_final:g} s (chosen to cover the "
            "settling target). Adjust if you want a different horizon."
        )
    elif tf_src == PROV_CLAMPED:
        msgs.append(f"ADJUSTED simulation horizon to a safety bound: t_final = {spec.t_final:g} s.")

    for key, src in prov.items():
        if key.startswith("scenario_suggested:") and src == PROV_LLM:
            name = key.split(":", 1)[1]
            msgs.append(
                f"SUGGESTION: a '{name}' test may be relevant but you did not request it — "
                "offer it to the engineer; it is NOT currently included."
            )
    return msgs


def spec_sanity_advisories(spec: DesignSpec) -> list[dict[str, str]]:
    """Motor-independent 'is this a realistic number?' checks with reasons + fixes.

    Complements the physics-based feasibility report (which is motor-specific) by
    catching values that are simply unusual/unrealistic regardless of the plant, so
    the agent can push back and suggest sensible ranges instead of silently accepting.
    """
    out: list[dict[str, str]] = []

    def add(code: str, message: str, suggestion: str) -> None:
        out.append({"code": code, "message": message, "suggestion": suggestion})

    hc = spec.hard_constraints

    sse = hc.get("steady_state_error")
    if sse and sse[0] in {"<=", "<"} and sse[1] < 0.005:
        add(
            "SSE_VERY_TIGHT",
            f"A steady-state error tolerance of {sse[1]*100:g}% is extremely tight for a "
            "simulated speed loop and may be dominated by numerical noise.",
            "Consider a tolerance of 1–5% (0.01–0.05) unless precision tracking is essential.",
        )

    os_ = hc.get("overshoot_pct")
    if os_ and os_[0] in {"<=", "<"} and os_[1] == 0.0:
        add(
            "OVERSHOOT_ZERO",
            "Requiring exactly 0% overshoot forces an over-damped response and usually "
            "conflicts with fast settling.",
            "Allow a small margin (e.g. 2–5%) unless zero overshoot is a hard requirement.",
        )

    st = hc.get("settling_time_s")
    if st and st[0] in {"<=", "<"} and st[1] < 0.05:
        add(
            "SETTLING_SUBMS",
            f"A settling time of {st[1]:g}s is faster than typical DC-motor speed loops "
            "can achieve without enormous control effort.",
            "Confirm the target; many motors settle in 0.2–5 s depending on inertia.",
        )

    rt, stt = hc.get("rise_time_s"), hc.get("settling_time_s")
    if rt and stt and rt[0] in {"<=", "<"} and stt[0] in {"<=", "<"} and rt[1] > stt[1]:
        add(
            "RISE_GT_SETTLING",
            f"Rise time ({rt[1]:g}s) is larger than settling time ({stt[1]:g}s), which is "
            "physically inconsistent (a response settles after it rises).",
            "Set rise time below settling time (often ~20–40% of it).",
        )

    return out


# --- Non-LLM template / regex parser (ablation baseline) ---

_METRIC_ALIASES = {
    "settling": "settling_time_s",
    "settling_time": "settling_time_s",
    "settle": "settling_time_s",
    "overshoot": "overshoot_pct",
    "ss_error": "steady_state_error",
    "steady_state_error": "steady_state_error",
    "rise": "rise_time_s",
    "rise_time": "rise_time_s",
    "itae": "ITAE",
    "iae": "IAE",
    "ise": "ISE",
    "effort": "control_effort",
    "saturation": "saturation_time_s",
    "recovery": "recovery_time_s",
    "recovery_time": "recovery_time_s",
    "recovery_time_s": "recovery_time_s",
}

_CONSTRAINT_RE = re.compile(
    r"(?P<metric>settling(?:_time)?|settle|overshoot|ss_error|steady_state_error|"
    r"rise(?:_time)?|itae|iae|ise|effort|saturation|recovery(?:_time)?|"
    r"settling_time_s|overshoot_pct|steady_state_error|rise_time_s|"
    r"ITAE|IAE|ISE|control_effort|saturation_time_s|recovery_time_s)"
    r"\s*(?P<op><=|>=|<|>|under|below|at\s+most|no\s+more\s+than)\s*"
    r"(?P<limit>\d+(?:\.\d+)?)\s*(?P<unit>%|s|sec|seconds)?",
    re.IGNORECASE,
)

_SCENARIO_RE = re.compile(
    r"(step_1rads|load_disturbance|plant_mismatch|noisy_measurement|"
    r"mismatch_load|mismatch_harsh|noise_low|noise_med|noise_high|"
    r"step|load|mismatch|noise)",
    re.IGNORECASE,
)

_OMEGA_RE = re.compile(
    r"(?:omega_ref|speed|reference)\s*(?:=|to|:)?\s*(?P<v>\d+(?:\.\d+)?)\s*"
    r"(?:rad/s|rpm|r\.?p\.?m\.?)?",
    re.IGNORECASE,
)

_VOLTAGE_RE = re.compile(
    r"(?:\|?u\|?|voltage)\s*(?:<=|under|max)?\s*(?P<v>\d+(?:\.\d+)?)\s*V?",
    re.IGNORECASE,
)


def _normalize_op(op: str) -> str:
    op = op.lower().strip()
    if op in {"under", "below", "at most", "no more than", "<"}:
        return "<=" if op != "<" else "<"
    if op in {"<=", ">=", "<", ">"}:
        return op
    return "<="


def _normalize_scenario_token(tok: str) -> str | None:
    t = tok.lower()
    mapping = {
        "step_1rads": "step_1rads",
        "step": "step_1rads",
        "load_disturbance": "load_disturbance",
        "load": "load_disturbance",
        "plant_mismatch": "plant_mismatch",
        "mismatch": "plant_mismatch",
        "noisy_measurement": "noisy_measurement",
        "noise": "noisy_measurement",
        "mismatch_load": "mismatch_load",
        "mismatch_harsh": "mismatch_harsh",
        "noise_low": "noise_low",
        "noise_med": "noise_med",
        "noise_high": "noise_high",
    }
    return mapping.get(t)


def parse_spec_template(text: str) -> DesignSpec:
    """Legacy regex parser kept only for offline debugging.

    Agents do **not** use this. Spec interpretation is OpenAI-only via
    ``agents.spec_agent.interpret_spec``. Prefer constructing a DesignSpec
    manually or calling the LLM interpreter.
    """
    hard: dict[str, tuple[str, float]] = {}
    for m in _CONSTRAINT_RE.finditer(text):
        alias = m.group("metric")
        metric = _METRIC_ALIASES.get(alias.lower(), alias)
        if metric not in ALLOWED_METRICS:
            # try case-sensitive names already in ALLOWED
            if alias in ALLOWED_METRICS:
                metric = alias
            else:
                continue
        op = _normalize_op(m.group("op"))
        limit = float(m.group("limit"))
        unit = (m.group("unit") or "").lower()
        if metric == "overshoot_pct" and unit in {"", "s", "sec", "seconds"} and limit <= 1.0:
            # "overshoot < 0.08" sometimes means 8%; keep as-is if user said %
            pass
        hard[metric] = (op, limit)

    scenarios: list[str] = []
    # Explicit "scenarios: a, b" block
    scen_block = re.search(r"scenarios?\s*:\s*([^\n;]+)", text, re.IGNORECASE)
    if scen_block:
        for part in re.split(r"[,\s]+", scen_block.group(1).strip()):
            if not part:
                continue
            name = _normalize_scenario_token(part)
            if name and name not in scenarios:
                scenarios.append(name)
    else:
        for m in _SCENARIO_RE.finditer(text):
            name = _normalize_scenario_token(m.group(1))
            if name and name not in scenarios:
                scenarios.append(name)

    # Keyword hints
    lower = text.lower()
    if "disturbance" in lower or "load" in lower:
        if "load_disturbance" not in scenarios:
            scenarios.append("load_disturbance")
    if "mismatch" in lower or "uncertain" in lower:
        if "plant_mismatch" not in scenarios:
            scenarios.append("plant_mismatch")
    if "noise" in lower:
        if "noisy_measurement" not in scenarios:
            scenarios.append("noisy_measurement")
    if not scenarios:
        scenarios = ["step_1rads"]

    extracted = extract_omega_ref_from_text(text)
    omega_ref = extracted if extracted is not None else 1.0
    if extracted is None:
        m = _OMEGA_RE.search(text)
        if m:
            omega_ref = float(m.group("v"))
            # Legacy regex may capture RPM without converting; fix when unit is rpm.
            span = text[m.start() : m.end()].lower()
            if "rpm" in span or "r.p.m" in span:
                omega_ref = rpm_to_rad_s(omega_ref)

    V_max = 12.0
    m = _VOLTAGE_RE.search(text)
    if m:
        V_max = float(m.group("v"))

    soft = {"ITAE": 1.0, "overshoot_pct": 0.05, "control_effort": 0.01}
    if "prefer low effort" in lower or "minimize effort" in lower:
        soft["control_effort"] = 0.2
    if "prefer tracking" in lower or "minimize itae" in lower:
        soft["ITAE"] = 2.0

    draft = DesignSpec(
        raw_spec=text,
        hard_constraints=hard,
        soft_preferences=soft,
        required_scenarios=scenarios,
        omega_ref=omega_ref,
        V_min=-V_max,
        V_max=V_max,
        source="template",
        notes="Parsed by regex template baseline (no LLM).",
    )
    return validate_and_clamp_design_spec(apply_text_derived_targets(draft))
