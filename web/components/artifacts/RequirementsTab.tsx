import { fmtNum, metricLabel } from "@/lib/format";
import type { SpecArtifact } from "@/lib/types";

import { Badge, Card, EmptyState, KeyValue } from "../ui";

export function RequirementsTab({ spec }: { spec?: SpecArtifact }) {
  if (!spec) {
    return (
      <EmptyState
        title="No requirements yet"
        hint="Tell the copilot the performance you need (settling time, overshoot, target speed…)."
      />
    );
  }
  const hard = spec.hard_constraints || {};
  const soft = spec.soft_preferences || {};

  return (
    <div className="space-y-4">
      {spec.raw_spec && (
        <Card
          title="Requirements"
          right={
            <Badge tone={spec.confirmed ? "ok" : "warn"}>
              {spec.confirmed ? "Confirmed" : "Proposed"}
            </Badge>
          }
        >
          <p className="text-sm italic text-slate-300">“{spec.raw_spec}”</p>
        </Card>
      )}

      <Card title="Hard constraints">
        {Object.keys(hard).length === 0 ? (
          <p className="text-sm text-slate-500">None specified.</p>
        ) : (
          <div className="divide-y divide-ink-700">
            {Object.entries(hard).map(([metric, c]) => (
              <KeyValue
                key={metric}
                label={metricLabel(metric)}
                value={
                  <span className="font-mono">
                    <span className="text-slate-400">{c.op} </span>
                    {fmtNum(c.limit)}
                  </span>
                }
              />
            ))}
          </div>
        )}
      </Card>

      <Card title="Operating point">
        <div className="divide-y divide-ink-700">
          <KeyValue label="Target speed ωref (rad/s)" value={fmtNum(spec.omega_ref)} mono />
          <KeyValue label="Voltage range (V)" value={`${fmtNum(spec.V_min)} … ${fmtNum(spec.V_max)}`} mono />
          <KeyValue label="Simulation horizon (s)" value={fmtNum(spec.t_final)} mono />
          {spec.required_scenarios && spec.required_scenarios.length > 0 && (
            <KeyValue
              label="Test scenarios"
              value={spec.required_scenarios.join(", ")}
            />
          )}
        </div>
      </Card>

      {Object.keys(soft).length > 0 && (
        <Card title="Soft preferences (weights)">
          <div className="divide-y divide-ink-700">
            {Object.entries(soft).map(([k, v]) => (
              <KeyValue key={k} label={metricLabel(k)} value={fmtNum(v)} mono />
            ))}
          </div>
        </Card>
      )}

      {spec.warnings && spec.warnings.length > 0 && (
        <Card title="Notes">
          <ul className="list-disc space-y-1 pl-5 text-sm text-warn">
            {spec.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
