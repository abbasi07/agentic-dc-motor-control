"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  agentChat,
  createJob,
  getJob,
  getWorkspace,
} from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import type {
  ActivityItem,
  ChatMessage,
  CopilotEvent,
  Workspace,
} from "@/lib/types";

import { ArtifactPanel } from "./ArtifactPanel";
import { ChatPane } from "./ChatPane";
import { TopBar } from "./TopBar";

const JOB_STORAGE_KEY = "copilot_job_id";

let activitySeq = 0;
function nextActivityId(): string {
  activitySeq += 1;
  return `act-${Date.now()}-${activitySeq}`;
}

function toolLabel(name: unknown): string {
  const s = String(name || "tool").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function Copilot() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [sending, setSending] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [booting, setBooting] = useState(true);

  const pushActivity = useCallback((item: Omit<ActivityItem, "id">) => {
    setActivity((prev) => [...prev.slice(-199), { ...item, id: nextActivityId() }]);
  }, []);

  const refreshWorkspace = useCallback(async (id: string) => {
    try {
      setWorkspace(await getWorkspace(id));
    } catch {
      /* transient — SSE will also push a snapshot */
    }
  }, []);

  // ----------------------------------------------------------------- //
  // Session bootstrap: resume the stored job or create a fresh one.
  // ----------------------------------------------------------------- //
  const loadJob = useCallback(
    async (id: string) => {
      const job = await getJob(id);
      setMessages(job.chat || []);
      await refreshWorkspace(id);
    },
    [refreshWorkspace],
  );

  const startNewSession = useCallback(async () => {
    setBooting(true);
    setBanner(null);
    try {
      const job = await createJob("dc_motor_ctms", "heuristic");
      window.localStorage.setItem(JOB_STORAGE_KEY, job.job_id);
      setActivity([]);
      setMessages([]);
      setJobId(job.job_id);
      await refreshWorkspace(job.job_id);
    } catch (e) {
      setBanner(errMsg(e));
    } finally {
      setBooting(false);
    }
  }, [refreshWorkspace]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const stored = window.localStorage.getItem(JOB_STORAGE_KEY);
      if (stored) {
        try {
          await loadJob(stored);
          if (!cancelled) {
            setJobId(stored);
            setBooting(false);
          }
          return;
        } catch {
          window.localStorage.removeItem(JOB_STORAGE_KEY);
        }
      }
      if (!cancelled) await startNewSession();
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ----------------------------------------------------------------- //
  // Live event stream (SSE). Left panel = workspace.updated; activity feed
  // = tool.*/run.status/refusal/error. Chat transcript stays authoritative
  // via the POST response, so message.delta only nudges a workspace refresh.
  // ----------------------------------------------------------------- //
  const handleEvent = useCallback(
    (event: CopilotEvent) => {
      const data = (event.data || {}) as Record<string, any>;
      switch (event.type) {
        case "workspace.updated":
          if (data.phase) setWorkspace(data as unknown as Workspace);
          break;
        case "tool.started":
          pushActivity({
            ts: event.ts,
            kind: "tool",
            tone: "running",
            title: `${toolLabel(data.tool)} …`,
            detail: summarizeArgs(data.args),
          });
          break;
        case "tool.finished":
          pushActivity({
            ts: event.ts,
            kind: "tool",
            tone: data.ok === false ? "warn" : "ok",
            title: toolLabel(data.tool),
            detail: summarizeResult(data.result),
          });
          break;
        case "run.status": {
          const status = String(data.status || "");
          pushActivity({
            ts: event.ts,
            kind: "run",
            tone:
              status === "completed"
                ? "ok"
                : status === "failed"
                  ? "danger"
                  : "running",
            title: `Design run: ${status}`,
            detail: data.error ? String(data.error) : undefined,
          });
          if (jobId && (status === "completed" || status === "failed")) {
            void refreshWorkspace(jobId);
          }
          break;
        }
        case "refusal":
          pushActivity({
            ts: event.ts,
            kind: "refusal",
            tone: "warn",
            title: "Off-topic — steering back",
            detail: typeof data.message === "string" ? data.message : undefined,
          });
          break;
        case "error":
          pushActivity({
            ts: event.ts,
            kind: "error",
            tone: "danger",
            title: "Error",
            detail:
              typeof data.error === "string" ? data.error : JSON.stringify(data),
          });
          break;
        case "message.delta":
          // The authoritative transcript arrives with the POST response; nothing to do.
          break;
      }
    },
    [jobId, pushActivity, refreshWorkspace],
  );

  const connection = useEventStream(jobId, {
    onEvent: handleEvent,
    enabled: !!jobId,
  });

  // ----------------------------------------------------------------- //
  // Send a chat turn to the OpenAI-driven Design Agent.
  // ----------------------------------------------------------------- //
  const sendingRef = useRef(false);
  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || !jobId || sendingRef.current) return;
      sendingRef.current = true;
      setSending(true);
      setBanner(null);
      setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
      try {
        const job = await agentChat(jobId, trimmed);
        setMessages(job.chat || []);
        await refreshWorkspace(jobId);
      } catch (e) {
        if (e instanceof ApiError && e.status === 503) {
          setBanner(
            "The chat agent needs an OpenAI key on the backend (OPENAI_API_KEY). " +
              "The left panel still reflects any computed state.",
          );
        } else if (e instanceof ApiError && e.status === 429) {
          setBanner(`Budget/rate limit: ${e.message}`);
        } else {
          setBanner(errMsg(e));
        }
      } finally {
        sendingRef.current = false;
        setSending(false);
      }
    },
    [jobId, refreshWorkspace],
  );

  return (
    <div className="flex h-screen flex-col bg-ink-950">
      <TopBar
        workspace={workspace}
        connection={connection}
        jobId={jobId}
        onNewSession={startNewSession}
      />
      {banner && (
        <div className="border-b border-warn/30 bg-warn/10 px-4 py-2 text-sm text-warn">
          {banner}
        </div>
      )}
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[1fr_minmax(380px,460px)]">
        {/* LEFT: dynamic artifact tabs (reflect-only). */}
        <ArtifactPanel workspace={workspace} booting={booting} />
        {/* RIGHT: chat + agent activity. */}
        <ChatPane
          messages={messages}
          activity={activity}
          sending={sending}
          connection={connection}
          disabled={!jobId || booting}
          onSend={sendMessage}
        />
      </div>
    </div>
  );
}

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return `${e.message}${e.status ? ` (HTTP ${e.status})` : ""}`;
  if (e instanceof Error) return e.message;
  return String(e);
}

function summarizeArgs(args: unknown): string | undefined {
  if (!args || typeof args !== "object") return undefined;
  const entries = Object.entries(args as Record<string, unknown>).slice(0, 3);
  if (!entries.length) return undefined;
  return entries
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ")
    .slice(0, 140);
}

function summarizeResult(result: unknown): string | undefined {
  if (result == null) return undefined;
  if (typeof result === "string") return result.slice(0, 140);
  if (typeof result === "object") {
    const r = result as Record<string, unknown>;
    if (typeof r.summary === "string") return r.summary.slice(0, 140);
    if (typeof r.status === "string") return String(r.status);
  }
  return undefined;
}
