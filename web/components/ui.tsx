import type { ReactNode } from "react";

export function Card({
  title,
  right,
  children,
  className = "",
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg border border-ink-700 bg-ink-850 ${className}`}
    >
      {(title || right) && (
        <header className="flex items-center justify-between gap-2 border-b border-ink-700 px-4 py-2.5">
          <h3 className="text-sm font-semibold text-slate-100">{title}</h3>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

type Tone = "info" | "ok" | "warn" | "danger" | "neutral" | "running";

const TONE_CLASS: Record<Tone, string> = {
  info: "bg-accent/15 text-accent border-accent/30",
  ok: "bg-ok/15 text-ok border-ok/30",
  warn: "bg-warn/15 text-warn border-warn/30",
  danger: "bg-danger/15 text-danger border-danger/30",
  neutral: "bg-ink-700 text-slate-300 border-ink-600",
  running: "bg-accent/10 text-accent border-accent/30",
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
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}
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
    <div className="flex items-baseline justify-between gap-3 py-1 text-sm">
      <span className="text-slate-400">{label}</span>
      <span className={`text-right text-slate-100 ${mono ? "font-mono" : ""}`}>
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
    <div className="flex h-full min-h-[200px] flex-col items-center justify-center gap-2 p-8 text-center">
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {hint && <p className="max-w-sm text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function PassFail({ pass }: { pass: boolean }) {
  return (
    <Badge tone={pass ? "ok" : "danger"}>{pass ? "PASS" : "FAIL"}</Badge>
  );
}
