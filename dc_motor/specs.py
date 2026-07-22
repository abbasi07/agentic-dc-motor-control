"""Formal DesignSpec: NL specs become validated constraints for orchestration."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

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

# Safety clamps: (min, max) inclusive for numeric DesignSpec fields / limits
METRIC_LIMIT_BOUNDS: dict[str, tuple[float, float]] = {
    "rise_time_s": (0.01, 10.0),
    "settling_time_s": (0.05, 15.0),
    "overshoot_pct": (0.0, 100.0),
    "steady_state_error": (1e-6, 1.0),
    "IAE": (0.0, 100.0),
    "ISE": (0.0, 100.0),
    "ITAE": (0.0, 100.0),
    "control_effort": (0.0, 500.0),
    "saturation_time_s": (0.0, 15.0),
    "recovery_time_s": (0.0, 10.0),
}


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


def validate_and_clamp_design_spec(spec: DesignSpec) -> DesignSpec:
    """Validate schema and clamp absurd limits. Mutates a copy-safe new warnings list."""
    warnings: list[str] = list(spec.warnings)

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
            warnings.append(f"Clamped {metric} limit to [{lo}, {hi}] -> {limit}")
        cleaned[metric] = (op, limit)

    if not cleaned:
        # Sensible defaults if NL parse yielded nothing
        cleaned = {
            "settling_time_s": ("<=", 2.0),
            "overshoot_pct": ("<=", 15.0),
            "steady_state_error": ("<=", 0.05),
        }
        warnings.append("No hard constraints found; applied default evaluation constraints.")

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

    omega_ref, ch = _clamp(spec.omega_ref, 0.01, 20.0)
    if ch:
        warnings.append(f"Clamped omega_ref -> {omega_ref}")

    V_max, ch = _clamp(abs(spec.V_max), 1.0, 48.0)
    if ch:
        warnings.append(f"Clamped |V_max| -> {V_max}")
    V_min = -V_max if spec.V_min < 0 else float(spec.V_min)
    V_min, ch2 = _clamp(V_min, -48.0, -1.0)
    if ch2:
        warnings.append(f"Clamped V_min -> {V_min}")

    t_final, ch = _clamp(spec.t_final, 0.5, 30.0)
    if ch:
        warnings.append(f"Clamped t_final -> {t_final}")

    max_iters = int(spec.max_design_iterations)
    if max_iters < 1:
        max_iters = 1
        warnings.append("max_design_iterations raised to 1")
    if max_iters > 20:
        max_iters = 20
        warnings.append("max_design_iterations capped at 20")

    return DesignSpec(
        raw_spec=spec.raw_spec,
        hard_constraints=cleaned,
        soft_preferences=prefs,
        required_scenarios=scenarios,
        omega_ref=omega_ref,
        V_min=V_min,
        V_max=V_max,
        t_final=t_final,
        max_design_iterations=max_iters,
        stop_on_pass=bool(spec.stop_on_pass),
        source=spec.source,
        notes=spec.notes,
        warnings=warnings,
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
    )
    return validate_and_clamp_design_spec(draft)


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
    r"(?:omega_ref|speed|reference)\s*(?:=|to|:)?\s*(?P<v>\d+(?:\.\d+)?)\s*(?:rad/s)?",
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

    omega_ref = 1.0
    m = _OMEGA_RE.search(text)
    if m:
        omega_ref = float(m.group("v"))

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
    return validate_and_clamp_design_spec(draft)
