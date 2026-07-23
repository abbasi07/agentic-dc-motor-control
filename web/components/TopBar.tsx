"use client";

import { useEffect, useState } from "react";

import { getApiKey, setApiKey } from "@/lib/api";
import type { ConnectionState } from "@/lib/sse";
import { PHASE_ORDER, type Phase, type Workspace } from "@/lib/types";
import { titleCase } from "@/lib/format";

const PHASE_SHORT: Record<Phase, string> = {
  greeting: "Motor",
  motor_negotiation: "Motor",
  motor_agreed: "Motor",
  spec_negotiation: "Requirements",
  controller_selection: "Controller",
  designing: "Design",
  results_review: "Results",
  exported: "Export",
};

// Collapse the 8 internal phases into 5 visible stages for the stepper.
const STAGES: { label: string; phases: Phase[] }[] = [
  { label: "Motor", phases: ["greeting", "motor_negotiation", "motor_agreed"] },
  { label: "Requirements", phases: ["spec_negotiation"] },
  { label: "Controller", phases: ["controller_selection"] },
  { label: "Design", phases: ["designing"] },
  { label: "Results", phases: ["results_review", "exported"] },
];

function stageIndex(phase: Phase): number {
  const idx = STAGES.findIndex((s) => s.phases.includes(phase));
  return idx < 0 ? 0 : idx;
}

export function TopBar({
  workspace,
  connection,
  jobId,
  onNewSession,
}: {
  workspace: Workspace | null;
  connection: ConnectionState;
  jobId: string | null;
  onNewSession: () => void;
}) {
  const phase = (workspace?.phase || "greeting") as Phase;
  const current = stageIndex(phase);

  return (
    <header className="border-b border-ink-700 bg-ink-900">
      <div className="flex items-center justify-between gap-4 px-4 py-2.5">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-accent/20 text-accent">
            <span className="text-lg font-bold">⌁</span>
          </div>
          <div>
            <h1 className="text-sm font-semibold text-slate-100">
              Control Design Copilot
            </h1>
            <p className="text-[11px] text-slate-500">
              Chat-first DC-motor controller design · simulation only
            </p>
          </div>
        </div>

        <PhaseStepper current={current} />

        <div className="flex items-center gap-3">
          <ConnectionDot state={connection} />
          <ApiKeyButton />
          <button
            onClick={onNewSession}
            className="rounded-md border border-ink-600 bg-ink-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-ink-700"
          >
            New session
          </button>
        </div>
      </div>
    </header>
  );
}

function PhaseStepper({ current }: { current: number }) {
  return (
    <ol className="hidden items-center gap-1 md:flex">
      {STAGES.map((stage, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <li key={stage.label} className="flex items-center gap-1">
            <div
              className={[
                "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-colors",
                active
                  ? "bg-accent/20 text-accent"
                  : done
                    ? "text-ok"
                    : "text-slate-500",
              ].join(" ")}
            >
              <span
                className={[
                  "flex h-4 w-4 items-center justify-center rounded-full text-[10px]",
                  active
                    ? "bg-accent text-ink-950"
                    : done
                      ? "bg-ok text-ink-950"
                      : "bg-ink-700 text-slate-400",
                ].join(" ")}
              >
                {done ? "✓" : i + 1}
              </span>
              {stage.label}
            </div>
            {i < STAGES.length - 1 && (
              <span className="text-ink-600">›</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function ConnectionDot({ state }: { state: ConnectionState }) {
  const map: Record<ConnectionState, { color: string; label: string; pulse: boolean }> =
    {
      idle: { color: "bg-slate-500", label: "idle", pulse: false },
      connecting: { color: "bg-warn", label: "connecting", pulse: true },
      open: { color: "bg-ok", label: "live", pulse: false },
      closed: { color: "bg-slate-500", label: "offline", pulse: false },
      error: { color: "bg-danger", label: "stream error", pulse: false },
    };
  const s = map[state];
  return (
    <div
      className="flex items-center gap-1.5 text-[11px] text-slate-400"
      title={`Event stream: ${s.label}`}
    >
      <span
        className={`h-2 w-2 rounded-full ${s.color} ${s.pulse ? "animate-pulse-dot" : ""}`}
      />
      {s.label}
    </div>
  );
}

function ApiKeyButton() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");

  useEffect(() => {
    if (open) setValue(getApiKey());
  }, [open]);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="rounded-md border border-ink-600 bg-ink-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-ink-700"
        title="Set the API key (Authorization: Bearer)"
      >
        API key
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-2 w-72 rounded-lg border border-ink-700 bg-ink-850 p-3 shadow-xl">
          <label className="mb-1 block text-xs text-slate-400">
            Authorization: Bearer
          </label>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-full rounded-md border border-ink-600 bg-ink-900 px-2 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-accent"
            placeholder="dev-local-key"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              onClick={() => setOpen(false)}
              className="rounded-md px-2 py-1 text-xs text-slate-400 hover:text-slate-200"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                setApiKey(value);
                setOpen(false);
              }}
              className="rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-ink-950 hover:opacity-90"
            >
              Save
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
