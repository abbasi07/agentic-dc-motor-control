"use client";

import { useState } from "react";

import { formatCopyBlock, formatCopyLines } from "@/lib/clipboard";
import { fmtNum, fmtPct, metricLabel } from "@/lib/format";
import type {
  PlotsArtifact,
  ResultsArtifact,
  ScenarioResult,
} from "@/lib/types";

import { Badge, Card, EmptyState, KeyValue, PassFail } from "../ui";
import { TrajectoryChart } from "./TrajectoryChart";

export function ResultsTab({
  results,
  plots,
}: {
  results?: ResultsArtifact;
  plots?: PlotsArtifact;
}) {
  const series = plots?.series || [];
  const scenarios = results?.scenarios || [];
  const [activeScenario, setActiveScenario] = useState(0);

  if (!results && series.length === 0) {
    return (
      <EmptyState
        title="No results yet"
        hint="Ask the copilot to design a controller. Metrics, pass/fail, and step-response plots will appear here."
      />
    );
  }

  const summary = results?.summary;
  const scenario = scenarios[activeScenario];
  const plotSeries =
    series.find((s) => s.name === scenario?.name) || series[activeScenario] || series[0];

  const summaryTitle = results?.controller || "Design result";
  const summaryCopy = formatCopyBlock(summaryTitle, [
    ...(summary
      ? [
          {
            label: "Constraints",
            value: summary.all_constraints_pass ? "All constraints pass" : "Constraints failed",
          },
          {
            label: "Scenarios passed",
            value: `${summary.n_scenarios_pass ?? "—"} / ${summary.n_scenarios ?? "—"}`,
          },
          { label: "Pass rate", value: fmtPct((summary.pass_rate ?? 0) * 100) },
          { label: "Mean score", value: fmtNum(summary.mean_scalar_score) },
          { label: "Worst-case ITAE", value: fmtNum(summary.worst_case_ITAE) },
        ]
      : []),
    ...(results?.session_status
      ? [{ label: "Session status", value: results.session_status }]
      : []),
  ]);

  const scenarioCopy = scenario
    ? formatScenarioCopy(scenario)
    : "";

  const rationaleCopy = results?.rationale
    ? formatCopyLines("Design rationale", [results.rationale])
    : "";

  return (
    <div className="space-y-4">
      <Card
        title={summaryTitle}
        copyText={summaryCopy}
        right={
          summary ? (
            <Badge tone={summary.all_constraints_pass ? "ok" : "danger"}>
              {summary.all_constraints_pass ? "All constraints pass" : "Constraints failed"}
            </Badge>
          ) : undefined
        }
      >
        {summary && (
          <div className="grid grid-cols-2 gap-x-4">
            <KeyValue
              label="Scenarios passed"
              value={`${summary.n_scenarios_pass ?? "—"} / ${summary.n_scenarios ?? "—"}`}
              mono
            />
            <KeyValue label="Pass rate" value={fmtPct((summary.pass_rate ?? 0) * 100)} mono />
            <KeyValue label="Mean score" value={fmtNum(summary.mean_scalar_score)} mono />
            <KeyValue label="Worst-case ITAE" value={fmtNum(summary.worst_case_ITAE)} mono />
          </div>
        )}
        {results?.session_status && (
          <p className="mt-2 text-xs text-slate-500">
            Session status: <span className="font-mono">{results.session_status}</span>
          </p>
        )}
      </Card>

      {scenarios.length > 0 && (
        <Card title="Scenario metrics" copyText={scenarioCopy}>
          {scenarios.length > 1 && (
            <div className="mb-3 flex flex-wrap gap-1">
              {scenarios.map((s, i) => (
                <button
                  key={s.name || i}
                  onClick={() => setActiveScenario(i)}
                  className={[
                    "rounded-lg px-2.5 py-1 text-xs font-medium transition-colors",
                    i === activeScenario
                      ? "bg-ink-800 text-cloud"
                      : "text-slate-400 hover:text-cloud/80",
                  ].join(" ")}
                >
                  {s.name || `Scenario ${i + 1}`}
                </button>
              ))}
            </div>
          )}
          {scenario && <ScenarioMetrics scenario={scenario} />}
        </Card>
      )}

      {plotSeries && (
        <Card title={`Step response — ${plotSeries.name || "scenario"}`}>
          <TrajectoryChart series={plotSeries} />
        </Card>
      )}

      {results?.rationale && (
        <Card title="Design rationale" copyText={rationaleCopy}>
          <pre className="whitespace-pre-wrap font-sans text-xs leading-relaxed text-slate-300">
            {results.rationale}
          </pre>
        </Card>
      )}
    </div>
  );
}

function formatScenarioCopy(scenario: ScenarioResult): string {
  const checks = scenario.constraints?.checks || {};
  const metrics = scenario.metrics || {};
  const checkRows = Object.entries(checks).map(([metric, c]) => ({
    label: metricLabel(metric),
    value: `${fmtNum(c.value)} ${c.op} ${fmtNum(c.limit)} (${c.pass ? "PASS" : "FAIL"})`,
  }));
  const metricRows = Object.entries(metrics).map(([k, v]) => ({
    label: metricLabel(k),
    value: fmtNum(v),
  }));
  return formatCopyBlock(`Scenario metrics — ${scenario.name || "scenario"}`, [
    ...checkRows,
    ...metricRows,
  ]);
}

function ScenarioMetrics({ scenario }: { scenario: ScenarioResult }) {
  const metrics = scenario.metrics || {};
  const checks = scenario.constraints?.checks || {};

  return (
    <div className="space-y-3">
      {Object.keys(checks).length > 0 && (
        <div className="space-y-1.5">
          {Object.entries(checks).map(([metric, c]) => (
            <div
              key={metric}
              className="flex items-center justify-between gap-2 rounded-lg border border-ink-700/80 bg-ink-900/80 px-3 py-2"
            >
              <span className="text-sm text-slate-300">{metricLabel(metric)}</span>
              <div className="flex items-center gap-2 font-mono text-xs text-slate-200">
                <span>{fmtNum(c.value)}</span>
                <span className="text-slate-500">
                  {c.op} {fmtNum(c.limit)}
                </span>
                <PassFail pass={c.pass} />
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="divide-y divide-ink-700">
        {Object.entries(metrics).map(([k, v]) => (
          <KeyValue key={k} label={metricLabel(k)} value={fmtNum(v)} mono />
        ))}
      </div>
    </div>
  );
}
