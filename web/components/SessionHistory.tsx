"use client";

import { useEffect, useMemo, useState } from "react";

import { ApiError, deleteJob, listJobs } from "@/lib/api";
import type { JobPublic } from "@/lib/types";

export function SessionHistory({
  currentJobId,
  onOpen,
  onNew,
  onClose,
  onDeletedCurrent,
}: {
  currentJobId: string | null;
  onOpen: (jobId: string) => void;
  onNew: () => void;
  onClose: () => void;
  /** Called when the active session was deleted so the app can start a fresh one. */
  onDeletedCurrent: () => void;
}) {
  const [jobs, setJobs] = useState<JobPublic[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await listJobs();
        if (!cancelled) setJobs(res.jobs || []);
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e),
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const sorted = [...jobs].sort(
      (a, b) => Date.parse(b.updated_at || b.created_at) - Date.parse(a.updated_at || a.created_at),
    );
    if (!q) return sorted;
    return sorted.filter((j) => {
      const title = sessionTitle(j).toLowerCase();
      const motor = String(j.motor?.name || "").toLowerCase();
      return title.includes(q) || motor.includes(q) || j.job_id.toLowerCase().includes(q);
    });
  }, [jobs, query]);

  const handleDelete = async (jobId: string) => {
    setDeletingId(jobId);
    setError(null);
    try {
      await deleteJob(jobId);
      setJobs((prev) => prev.filter((j) => j.job_id !== jobId));
      setConfirmId(null);
      if (jobId === currentJobId) {
        onDeletedCurrent();
      }
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e),
      );
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 space-y-2 border-b border-ink-700/80 px-3 py-2.5">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded px-1.5 py-1 text-xs text-slate-400 hover:bg-ink-850 hover:text-cloud"
            title="Back to chat"
          >
            ←
          </button>
          <span className="text-xs font-medium text-cloud/90">Chat History</span>
          <button
            type="button"
            onClick={onNew}
            className="ml-auto rounded px-2 py-1 text-xs font-medium text-slate-300 hover:bg-ink-850 hover:text-cloud"
          >
            New Chat
          </button>
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search chats"
          className="w-full rounded border border-ink-700/80 bg-ink-950/60 px-2.5 py-1.5 text-xs text-cloud outline-none placeholder:text-slate-600 focus:border-ink-600"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {loading && (
          <p className="px-4 py-6 text-center text-xs text-slate-500">Loading…</p>
        )}
        {error && (
          <p className="px-4 py-3 text-center text-xs text-warn">{error}</p>
        )}
        {!loading && !error && filtered.length === 0 && (
          <p className="px-4 py-6 text-center text-xs text-slate-500">
            {query.trim() ? "No matching chats." : "No past chats yet."}
          </p>
        )}
        {!loading &&
          filtered.map((job) => {
            const active = job.job_id === currentJobId;
            const confirming = confirmId === job.job_id;
            const deleting = deletingId === job.job_id;

            if (confirming) {
              return (
                <div
                  key={job.job_id}
                  className="border-l-2 border-l-danger/70 bg-danger/5 px-3 py-2.5"
                >
                  <p className="text-[13px] leading-snug text-cloud/90">
                    Delete this chat permanently?
                  </p>
                  <p className="mt-0.5 truncate text-[11px] text-slate-500">
                    {sessionTitle(job)}
                  </p>
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      type="button"
                      disabled={deleting}
                      onClick={() => void handleDelete(job.job_id)}
                      className="rounded bg-danger/90 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-danger disabled:opacity-50"
                    >
                      {deleting ? "Deleting…" : "Delete"}
                    </button>
                    <button
                      type="button"
                      disabled={deleting}
                      onClick={() => setConfirmId(null)}
                      className="rounded px-2.5 py-1 text-[11px] text-slate-400 hover:bg-ink-850 hover:text-cloud disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              );
            }

            return (
              <div
                key={job.job_id}
                className={[
                  "group flex items-stretch border-l-2 transition-colors",
                  active
                    ? "border-l-accent bg-ink-850/90"
                    : "border-l-transparent hover:bg-ink-850/50",
                ].join(" ")}
              >
                <button
                  type="button"
                  onClick={() => onOpen(job.job_id)}
                  className="min-w-0 flex-1 flex-col gap-0.5 px-3 py-2.5 text-left"
                >
                  <span
                    className={[
                      "block truncate text-[13px] leading-snug",
                      active ? "font-medium text-cloud" : "text-cloud/85",
                    ].join(" ")}
                  >
                    {sessionTitle(job)}
                  </span>
                  <span className="block truncate text-[11px] text-slate-500">
                    {relativeTime(job.updated_at || job.created_at)}
                    {job.motor?.name ? ` · ${job.motor.name}` : ""}
                    {job.scorecard_summary ? " · results" : ""}
                  </span>
                </button>
                <button
                  type="button"
                  title="Delete chat"
                  aria-label="Delete chat"
                  onClick={(e) => {
                    e.stopPropagation();
                    setConfirmId(job.job_id);
                  }}
                  className="shrink-0 px-2.5 text-slate-600 opacity-0 transition-opacity hover:text-danger group-hover:opacity-100 focus:opacity-100"
                >
                  <TrashIcon />
                </button>
              </div>
            );
          })}
      </div>
    </div>
  );
}

function TrashIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden className="block">
      <path
        d="M3.5 4.5h9M6 4.5V3.5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1M5.5 4.5l.5 8h4l.5-8"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function sessionTitle(job: JobPublic): string {
  const firstUser = job.chat?.find((m) => m.role === "user")?.content?.trim();
  if (firstUser) return firstUser.replace(/\s+/g, " ").slice(0, 80);
  if (job.nl_spec?.trim()) return job.nl_spec.trim().replace(/\s+/g, " ").slice(0, 80);
  if (job.motor?.name) return String(job.motor.name);
  return "New design session";
}

function relativeTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 14) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
