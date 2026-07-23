// Small presentation helpers. These NEVER compute engineering numbers — they only format
// values the deterministic backend already produced (grounding invariant).

export function fmtNum(v: unknown, digits = 4): string {
  if (v === null || v === undefined) return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  // Compact but readable: use significant digits for small/large, fixed otherwise.
  if (n !== 0 && (Math.abs(n) < 1e-3 || Math.abs(n) >= 1e5)) {
    return n.toExponential(2);
  }
  return Number(n.toFixed(digits)).toString();
}

export function fmtPct(v: unknown, digits = 1): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

export function titleCase(s: string): string {
  return s
    .replace(/[_\-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

const METRIC_LABELS: Record<string, string> = {
  rise_time_s: "Rise time (s)",
  settling_time_s: "Settling time (s)",
  overshoot_pct: "Overshoot (%)",
  steady_state_error: "Steady-state error",
  IAE: "IAE",
  ISE: "ISE",
  ITAE: "ITAE",
  control_effort: "Control effort",
  saturation_time_s: "Saturation time (s)",
  recovery_time_s: "Recovery time (s)",
};

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] || titleCase(key);
}

const PARAM_LABELS: Record<string, string> = {
  J: "J — inertia (kg·m²)",
  b: "b — viscous friction (N·m·s/rad)",
  K: "K — torque/back-EMF (SI)",
  R: "R — resistance (Ω)",
  L: "L — inductance (H)",
  V_max: "V_max (V)",
  V_min: "V_min (V)",
};

export function paramLabel(key: string): string {
  return PARAM_LABELS[key] || key;
}

/**
 * Uniformly downsample parallel arrays to at most `maxPoints` for charting. Trajectory
 * series can be thousands of points (3001 for a 3 s run at 1 ms); SVG charts choke past a
 * few hundred. Keeps first + last and every k-th sample in between.
 */
export function downsample<T>(arr: T[], maxPoints = 400): T[] {
  if (arr.length <= maxPoints) return arr;
  const step = Math.ceil(arr.length / maxPoints);
  const out: T[] = [];
  for (let i = 0; i < arr.length; i += step) out.push(arr[i]);
  if (out[out.length - 1] !== arr[arr.length - 1]) out.push(arr[arr.length - 1]);
  return out;
}
