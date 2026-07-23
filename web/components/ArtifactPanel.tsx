"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { ArtifactKey, Workspace } from "@/lib/types";

import { EmptyState } from "./ui";
import { MotorTab } from "./artifacts/MotorTab";
import { RequirementsTab } from "./artifacts/RequirementsTab";
import { FeasibilityTab } from "./artifacts/FeasibilityTab";
import { ResultsTab } from "./artifacts/ResultsTab";
import { ExportTab } from "./artifacts/ExportTab";

type TabId = "motor" | "requirements" | "feasibility" | "results" | "export";

// Each visible tab is backed by one or more workspace artifact keys. A tab APPEARS only
// once at least one of its artifacts exists — panels fill in as the conversation reaches
// each stage (dynamic UI = backend-driven presence, fixed component types).
const TABS: { id: TabId; label: string; keys: ArtifactKey[] }[] = [
  { id: "motor", label: "Motor", keys: ["motor"] },
  { id: "requirements", label: "Requirements", keys: ["spec"] },
  { id: "feasibility", label: "Feasibility", keys: ["feasibility"] },
  { id: "results", label: "Results & Plots", keys: ["results", "plots"] },
  { id: "export", label: "Export", keys: ["certification", "export"] },
];

export function ArtifactPanel({
  workspace,
  booting,
}: {
  workspace: Workspace | null;
  booting: boolean;
}) {
  const available = useMemo(() => {
    const arts = workspace?.artifacts || {};
    return TABS.filter((t) => t.keys.some((k) => arts[k] != null));
  }, [workspace]);

  const [active, setActive] = useState<TabId | null>(null);
  const lastMaxRef = useRef(-1);

  // Auto-follow the workflow: jump to the newest (most advanced) tab as it appears, but
  // otherwise respect the user's manual selection.
  useEffect(() => {
    if (available.length === 0) {
      setActive(null);
      return;
    }
    const maxIdx = Math.max(
      ...available.map((t) => TABS.findIndex((x) => x.id === t.id)),
    );
    const activeStillThere = active && available.some((t) => t.id === active);
    if (!activeStillThere || maxIdx > lastMaxRef.current) {
      setActive(TABS[maxIdx].id);
    }
    lastMaxRef.current = maxIdx;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [available]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-ink-950/40">
      {/* Tab strip */}
      <div className="flex items-center gap-1 overflow-x-auto border-b border-ink-700/80 bg-ink-900/60 px-3 py-2.5">
        {TABS.map((t) => {
          const present = available.some((a) => a.id === t.id);
          if (!present) return null;
          return (
            <button
              key={t.id}
              onClick={() => setActive(t.id)}
              className={[
                "whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium transition-colors",
                active === t.id
                  ? "bg-ink-850 text-cloud shadow-panel"
                  : "text-slate-400 hover:text-cloud/80",
              ].join(" ")}
            >
              {t.label}
            </button>
          );
        })}
        {available.length === 0 && (
          <span className="px-2 py-1 text-xs text-slate-500">
            Artifacts appear here as the design progresses
          </span>
        )}
      </div>

      {/* Tab body */}
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <TabBody active={active} workspace={workspace} booting={booting} />
      </div>
    </div>
  );
}

function TabBody({
  active,
  workspace,
  booting,
}: {
  active: TabId | null;
  workspace: Workspace | null;
  booting: boolean;
}) {
  const arts = workspace?.artifacts || {};
  if (!active) {
    return (
      <EmptyState
        title={booting ? "Starting a session…" : "Your workspace is empty"}
        hint={
          booting
            ? undefined
            : "Describe your motor in the chat on the right. The Motor, Requirements, Feasibility, Results, and Export panels will appear here as we go."
        }
      />
    );
  }
  switch (active) {
    case "motor":
      return <MotorTab motor={arts.motor} />;
    case "requirements":
      return <RequirementsTab spec={arts.spec} />;
    case "feasibility":
      return <FeasibilityTab feasibility={arts.feasibility} />;
    case "results":
      return <ResultsTab results={arts.results} plots={arts.plots} />;
    case "export":
      return (
        <ExportTab
          certification={arts.certification}
          exportArtifact={arts.export}
          jobId={workspace?.job_id}
        />
      );
    default:
      return null;
  }
}
