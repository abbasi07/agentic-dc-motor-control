import { fmtNum, titleCase } from "@/lib/format";
import type { FeasibilityArtifact, FeasibilityIssue } from "@/lib/types";

import { Badge, Card, EmptyState, KeyValue } from "../ui";

const SEV_TONE: Record<string, "danger" | "warn" | "info"> = {
  error: "danger",
  warning: "warn",
  info: "info",
};

export function FeasibilityTab({
  feasibility,
}: {
  feasibility?: FeasibilityArtifact;
}) {
  if (!feasibility) {
    return (
      <EmptyState
        title="No feasibility check yet"
        hint="Once a motor and requirements exist, the physics-based feasibility report appears here."
      />
    );
  }
  const feasible = feasibility.feasible !== false;
  const issues = (feasibility.issues || []) as FeasibilityIssue[];
  const chars = feasibility.characteristics || {};

  return (
    <div className="space-y-4">
      <Card
        title="Physics feasibility"
        right={
          <Badge tone={feasible ? "ok" : "danger"}>
            {feasible ? "Feasible" : "Not achievable"}
          </Badge>
        }
      >
        <p className="text-sm text-slate-400">
          Deterministic check of the requirements against this motor&apos;s physical
          limits. The copilot cannot override this gate.
        </p>
      </Card>

      <Card title={`Findings (${issues.length})`}>
        {issues.length === 0 ? (
          <p className="text-sm text-ok">No issues — the targets are physically reachable.</p>
        ) : (
          <ul className="space-y-3">
            {issues.map((issue, i) => (
              <li
                key={i}
                className="rounded-md border border-ink-700 bg-ink-900 p-3"
              >
                <div className="mb-1 flex items-center gap-2">
                  <Badge tone={SEV_TONE[issue.severity || "info"] || "info"}>
                    {(issue.severity || "info").toUpperCase()}
                  </Badge>
                  {issue.code && (
                    <span className="font-mono text-[11px] text-slate-500">
                      {issue.code}
                    </span>
                  )}
                </div>
                <p className="text-sm text-slate-200">{issue.message}</p>
                {issue.suggestion && (
                  <p className="mt-1 text-xs text-slate-400">→ {issue.suggestion}</p>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {Object.keys(chars).length > 0 && (
        <Card title="Motor characteristics used">
          <div className="divide-y divide-ink-700">
            {Object.entries(chars).map(([k, v]) => (
              <KeyValue
                key={k}
                label={titleCase(k.replace(/_rad_s$/, " (rad/s)").replace(/_s$/, " (s)"))}
                value={typeof v === "number" ? fmtNum(v) : String(v)}
                mono
              />
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
