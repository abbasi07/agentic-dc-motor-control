import type { ReactNode } from "react";

import { CopyButton } from "./CopyButton";

export function Card({
  title,
  right,
  copyText,
  children,
  className = "",
}: {
  title?: ReactNode;
  right?: ReactNode;
  /** When set, shows a copy control in the card header for this section's text. */
  copyText?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-ink-700/80 bg-ink-850/90 shadow-panel ${className}`}
    >
      {(title || right || copyText) && (
        <header className="flex items-center justify-between gap-2 border-b border-ink-700/80 px-4 py-3">
          <h3 className="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight text-cloud">
            {title}
          </h3>
          <div className="flex shrink-0 items-center gap-2">
            {right}
            {copyText ? <CopyButton text={copyText} /> : null}
          </div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

type Tone = "info" | "ok" | "warn" | "danger" | "neutral" | "running";

const TONE_CLASS: Record<Tone, string> = {
  info: "bg-violet/15 text-violet border-violet/30",
  ok: "bg-ok/15 text-ok border-ok/30",
  warn: "bg-warn/15 text-warn border-warn/30",
  danger: "bg-danger/15 text-danger border-danger/30",
  neutral: "bg-ink-800 text-slate-300 border-ink-600",
  running: "bg-violet/10 text-violet border-violet/30",
};

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: Tone;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium tracking-wide ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

export function KeyValue({
  label,
  value,
  mono = false,
}: {
  label: ReactNode;
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 text-sm">
      <span className="text-slate-400">{label}</span>
      <span className={`text-right text-cloud ${mono ? "font-mono text-[13px]" : ""}`}>
        {value}
      </span>
    </div>
  );
}

export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-2 p-8 text-center animate-fade-up">
      <p className="text-sm font-medium text-cloud/80">{title}</p>
      {hint && <p className="max-w-sm text-xs leading-relaxed text-slate-500">{hint}</p>}
    </div>
  );
}

export function PassFail({ pass }: { pass: boolean }) {
  return (
    <Badge tone={pass ? "ok" : "danger"}>{pass ? "PASS" : "FAIL"}</Badge>
  );
}
