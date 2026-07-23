import { fmtNum, paramLabel, titleCase } from "@/lib/format";
import type { MotorArtifact } from "@/lib/types";

import { Badge, Card, EmptyState, KeyValue } from "../ui";

const CHAR_LABELS: Record<string, string> = {
  dc_gain: "DC gain (rad/s per V)",
  omega_max_rad_s: "Max speed at V_max (rad/s)",
  tau_mech_s: "Mechanical time constant (s)",
  tau_elec_s: "Electrical time constant (s)",
  wn_rad_s: "Natural frequency ωn (rad/s)",
  zeta: "Damping ratio ζ",
  damping: "Damping",
};

export function MotorTab({ motor }: { motor?: MotorArtifact }) {
  if (!motor) {
    return <EmptyState title="No motor defined yet" hint="Describe your DC motor in the chat." />;
  }
  const params = motor.params || {};
  const units = motor.param_units || {};
  const chars = motor.characteristics || {};

  return (
    <div className="space-y-4">
      <Card
        title={motor.name || "DC motor"}
        right={
          <Badge tone={motor.confirmed ? "ok" : "warn"}>
            {motor.confirmed ? "Confirmed" : "Proposed"}
          </Badge>
        }
      >
        <div className="mb-3 flex flex-wrap gap-2 text-xs text-slate-400">
          {motor.source && <Badge tone="neutral">source: {motor.source}</Badge>}
          {motor.V_max != null && (
            <Badge tone="info">V_max {fmtNum(motor.V_max)} V</Badge>
          )}
          {motor.V_min != null && (
            <Badge tone="neutral">V_min {fmtNum(motor.V_min)} V</Badge>
          )}
        </div>
        <div className="divide-y divide-ink-700">
          {Object.entries(params).map(([k, v]) => (
            <KeyValue
              key={k}
              label={paramLabel(k)}
              value={
                <span>
                  {fmtNum(v)}
                  {units[k] ? (
                    <span className="ml-1 text-slate-500">{units[k]}</span>
                  ) : null}
                </span>
              }
              mono
            />
          ))}
        </div>
      </Card>

      {Object.keys(chars).length > 0 && (
        <Card title="Derived characteristics">
          <div className="divide-y divide-ink-700">
            {Object.entries(chars).map(([k, v]) => (
              <KeyValue
                key={k}
                label={CHAR_LABELS[k] || titleCase(k)}
                value={typeof v === "number" ? fmtNum(v) : String(v)}
                mono
              />
            ))}
          </div>
        </Card>
      )}

      {motor.warnings && motor.warnings.length > 0 && (
        <Card title="Notes on the numbers">
          <ul className="list-disc space-y-1 pl-5 text-sm text-warn">
            {motor.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
