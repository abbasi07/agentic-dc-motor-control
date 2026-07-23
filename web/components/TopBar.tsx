"use client";

import { useEffect, useState } from "react";

import { getApiKey, setApiKey } from "@/lib/api";
import type { ConnectionState } from "@/lib/sse";
import type { Phase, Workspace } from "@/lib/types";

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
  chatOpen,
  onToggleChat,
  onNewSession,
}: {
  workspace: Workspace | null;
  connection: ConnectionState;
  jobId: string | null;
  chatOpen: boolean;
  onToggleChat: () => void;
  onNewSession: () => void;
}) {
  const phase = (workspace?.phase || "greeting") as Phase;
  const current = stageIndex(phase);

  return (
    <header className="border-b border-ink-700/80 bg-ink-900/80 backdrop-blur-md">
      <div className="flex items-center justify-between gap-4 px-5 py-3">
        <div className="min-w-0">
          <h1 className="text-[15px] font-semibold tracking-tight text-cloud">
            Control Design
            <span className="ml-1.5 font-normal text-violet">Copilot</span>
          </h1>
          <p className="truncate text-[11px] text-slate-500">
            DC-motor speed control · simulation only
          </p>
        </div>

        <PhaseStepper current={current} />

        <div className="flex shrink-0 items-center gap-2.5">
          <ConnectionDot state={connection} />
          <ApiKeyButton />
          <button
            type="button"
            onClick={onToggleChat}
            aria-pressed={chatOpen}
            title={chatOpen ? "Hide chat pane" : "Show chat pane"}
            className="rounded-lg border border-ink-600 bg-ink-850 px-3 py-1.5 text-xs font-medium text-cloud/90 transition-colors hover:border-ink-600 hover:bg-ink-800"
          >
            {chatOpen ? "Hide chat" : "Show chat"}
          </button>
          <button
            onClick={onNewSession}
            className="rounded-lg border border-ink-600 bg-ink-850 px-3 py-1.5 text-xs font-medium text-cloud/90 transition-colors hover:border-ink-600 hover:bg-ink-800"
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
    <ol className="hidden items-center gap-0 md:flex">
      {STAGES.map((stage, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <li key={stage.label} className="flex items-center">
            <div
              className={[
                "flex items-center gap-2 rounded-lg px-2.5 py-1 text-xs font-medium transition-colors",
                active
                  ? "bg-accent/10 text-accent"
                  : done
                    ? "text-accent/80"
                    : "text-slate-500",
              ].join(" ")}
            >
              <span
                className={[
                  "flex h-5 w-5 items-center justify-center rounded-md text-[10px] font-semibold tabular-nums",
                  active
                    ? "bg-accent text-ink-950"
                    : done
                      ? "bg-accent/20 text-accent"
                      : "bg-ink-800 text-slate-500",
                ].join(" ")}
              >
                {i + 1}
              </span>
              {stage.label}
            </div>
            {i < STAGES.length - 1 && (
              <span
                className={[
                  "mx-0.5 h-px w-4",
                  i < current ? "bg-accent/40" : "bg-ink-700",
                ].join(" ")}
                aria-hidden
              />
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
        className={`h-1.5 w-1.5 rounded-full ${s.color} ${s.pulse ? "animate-pulse-dot" : ""}`}
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
        className="rounded-lg border border-ink-600 bg-ink-850 px-3 py-1.5 text-xs font-medium text-cloud/90 transition-colors hover:bg-ink-800"
        title="Set the API key (Authorization: Bearer)"
      >
        API key
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-2 w-72 rounded-xl border border-ink-700 bg-ink-850 p-3 shadow-xl shadow-black/40">
          <label className="mb-1.5 block text-xs text-slate-400">
            Authorization: Bearer
          </label>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-full rounded-lg border border-ink-600 bg-ink-950 px-2.5 py-1.5 font-mono text-xs text-cloud outline-none transition-colors focus:border-accent focus:shadow-glow"
            placeholder="dev-local-key"
          />
          <div className="mt-2.5 flex justify-end gap-2">
            <button
              onClick={() => setOpen(false)}
              className="rounded-lg px-2.5 py-1 text-xs text-slate-400 transition-colors hover:text-cloud"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                setApiKey(value);
                setOpen(false);
              }}
              className="rounded-lg bg-accent px-2.5 py-1 text-xs font-semibold text-ink-950 transition-opacity hover:opacity-90"
            >
              Save
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
