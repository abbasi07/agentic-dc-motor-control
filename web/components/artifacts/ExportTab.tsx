"use client";

import { useState } from "react";

import { downloadExport } from "@/lib/api";
import { formatCopyBlock, formatCopyLines } from "@/lib/clipboard";
import { fmtNum } from "@/lib/format";
import type { CertificationArtifact, ExportArtifact } from "@/lib/types";

import { Badge, Card, EmptyState, KeyValue } from "../ui";

export function ExportTab({
  certification,
  exportArtifact,
  jobId,
}: {
  certification?: CertificationArtifact;
  exportArtifact?: ExportArtifact;
  jobId?: string;
}) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!certification && !exportArtifact) {
    return (
      <EmptyState
        title="Nothing to export yet"
        hint="Once a design passes the certification gate, the certification decision and the export package appear here."
      />
    );
  }

  const allowed = certification?.allowed === true;

  const certCopy = certification
    ? formatCopyBlock("Certification gate", [
        { label: "Decision", value: allowed ? "ALLOW" : "BLOCK" },
        { label: "Reason", value: certification.reason },
        ...(certification.controller_name
          ? [{ label: "Controller", value: certification.controller_name }]
          : []),
        ...(certification.kind ? [{ label: "Family", value: certification.kind }] : []),
        ...(certification.timestamp_utc
          ? [{ label: "Certified at (UTC)", value: certification.timestamp_utc }]
          : []),
        ...Object.entries(certification.params || {}).map(([k, v]) => ({
          label: k,
          value: fmtNum(v),
        })),
      ])
    : "";

  const exportCopy = formatCopyLines("Export package", [
    exportArtifact?.status ? `Status: ${exportArtifact.status}` : "",
    exportArtifact?.path ? `Path: ${exportArtifact.path}` : "No package written yet.",
  ]);

  const handleDownload = async () => {
    if (!jobId) return;
    setDownloading(true);
    setError(null);
    try {
      const { url, filename } = await downloadExport(jobId);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="space-y-4">
      {certification && (
        <Card
          title="Certification gate"
          copyText={certCopy}
          right={
            <Badge tone={allowed ? "ok" : "danger"}>
              {allowed ? "ALLOW" : "BLOCK"}
            </Badge>
          }
        >
          <p className="text-sm text-slate-300">{certification.reason}</p>
          <div className="mt-3 divide-y divide-ink-700">
            {certification.controller_name && (
              <KeyValue label="Controller" value={certification.controller_name} mono />
            )}
            {certification.kind && (
              <KeyValue label="Family" value={certification.kind} mono />
            )}
            {certification.timestamp_utc && (
              <KeyValue label="Certified at (UTC)" value={certification.timestamp_utc} mono />
            )}
          </div>
          {certification.params && Object.keys(certification.params).length > 0 && (
            <div className="mt-3">
              <p className="mb-1 text-xs text-slate-400">Gains / parameters</p>
              <div className="divide-y divide-ink-700">
                {Object.entries(certification.params).map(([k, v]) => (
                  <KeyValue key={k} label={k} value={fmtNum(v)} mono />
                ))}
              </div>
            </div>
          )}
        </Card>
      )}

      <Card title="Export package" copyText={exportCopy}>
        <p className="mb-3 text-sm text-slate-400">
          The certification package bundles the controller parameters, the full scorecard,
          and a grounded rationale. Simulation certification only — not hardware.
        </p>
        {exportArtifact?.path ? (
          <div className="space-y-3">
            <KeyValue
              label="Status"
              value={<Badge tone="ok">{exportArtifact.status || "exported"}</Badge>}
            />
            <div className="break-all rounded-lg border border-ink-700/80 bg-ink-900 px-3 py-2 font-mono text-[11px] text-slate-400">
              {exportArtifact.path}
            </div>
            <button
              onClick={handleDownload}
              disabled={downloading || !jobId}
              className="w-full rounded-lg bg-accent px-3 py-2.5 text-sm font-semibold text-ink-950 transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {downloading ? "Preparing…" : "Download package (.zip)"}
            </button>
            {error && <p className="text-xs text-danger">{error}</p>}
          </div>
        ) : (
          <p className="text-sm text-slate-500">
            No package written yet. Ask the copilot to export once the design is certified.
          </p>
        )}
      </Card>
    </div>
  );
}
