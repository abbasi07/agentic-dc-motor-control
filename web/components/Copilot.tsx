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
const CHAT_WIDTH_KEY = "copilot_chat_width";
const CHAT_OPEN_KEY = "copilot_chat_open";
const DEFAULT_CHAT_WIDTH = 420;
const MIN_CHAT_WIDTH = 300;
const MAX_CHAT_WIDTH = 720;

let activitySeq = 0;
function nextActivityId(): string {
  activitySeq += 1;
  return `act-${Date.now()}-${activitySeq}`;
}

function toolLabel(name: unknown): string {
  const s = String(name || "tool").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function clampChatWidth(px: number): number {
  if (typeof window === "undefined") {
    return Math.min(MAX_CHAT_WIDTH, Math.max(MIN_CHAT_WIDTH, px));
  }
  const room = Math.max(MIN_CHAT_WIDTH, window.innerWidth - 360);
  return Math.min(MAX_CHAT_WIDTH, Math.min(room, Math.max(MIN_CHAT_WIDTH, px)));
}

export function Copilot() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [sending, setSending] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [booting, setBooting] = useState(true);
  const [chatWidth, setChatWidth] = useState(DEFAULT_CHAT_WIDTH);
  const [chatOpen, setChatOpen] = useState(true);
  const [resizing, setResizing] = useState(false);

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

  const setChatOpenPersisted = useCallback((open: boolean) => {
    setChatOpen(open);
    try {
      window.localStorage.setItem(CHAT_OPEN_KEY, open ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, []);

  const toggleChat = useCallback(() => {
    setChatOpenPersisted(!chatOpen);
  }, [chatOpen, setChatOpenPersisted]);

  // Restore persisted chat pane width + open/closed.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(CHAT_WIDTH_KEY);
      if (raw) {
        const n = Number(raw);
        if (Number.isFinite(n)) setChatWidth(clampChatWidth(n));
      }
      const openRaw = window.localStorage.getItem(CHAT_OPEN_KEY);
      if (openRaw === "0") setChatOpen(false);
      if (openRaw === "1") setChatOpen(true);
    } catch {
      /* ignore storage errors */
    }
  }, []);

  // Drag-to-resize the chat pane (desktop).
  useEffect(() => {
    if (!resizing) return;
    const onMove = (e: MouseEvent) => {
      const next = clampChatWidth(window.innerWidth - e.clientX);
      setChatWidth(next);
    };
    const onUp = () => {
      setResizing(false);
      setChatWidth((w) => {
        try {
          window.localStorage.setItem(CHAT_WIDTH_KEY, String(w));
        } catch {
          /* ignore */
        }
        return w;
      });
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [resizing]);

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

  const openSession = useCallback(
    async (id: string) => {
      if (!id || id === jobId) return;
      setBooting(true);
      setBanner(null);
      setActivity([]);
      try {
        window.localStorage.setItem(JOB_STORAGE_KEY, id);
        await loadJob(id);
        setJobId(id);
        setChatOpenPersisted(true);
      } catch (e) {
        setBanner(errMsg(e));
      } finally {
        setBooting(false);
      }
    },
    [jobId, loadJob, setChatOpenPersisted],
  );

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
        chatOpen={chatOpen}
        onToggleChat={toggleChat}
        onNewSession={startNewSession}
      />
      {banner && (
        <div className="border-b border-warn/25 bg-warn/10 px-5 py-2.5 text-sm text-warn">
          {banner}
        </div>
      )}
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        {/* LEFT: dynamic artifact tabs (reflect-only). */}
        <div className="min-h-0 min-w-0 flex-1">
          <ArtifactPanel workspace={workspace} booting={booting} />
        </div>
        {chatOpen ? (
          <>
            {/* Drag handle — desktop only. */}
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize chat pane"
              title="Drag to resize"
              onMouseDown={(e) => {
                e.preventDefault();
                setResizing(true);
              }}
              className={[
                "hidden shrink-0 cursor-col-resize lg:block",
                "w-1.5 bg-ink-700/50 transition-colors",
                "hover:bg-accent/50",
                resizing ? "bg-accent/70" : "",
              ].join(" ")}
            />
            {/* RIGHT: chat + agent activity (width adjustable on lg+). */}
            <div
              className="flex min-h-[42vh] w-full min-w-0 flex-col lg:min-h-0 lg:w-[var(--chat-pane-width)] lg:shrink-0"
              style={{ ["--chat-pane-width" as string]: `${chatWidth}px` }}
            >
              <ChatPane
                messages={messages}
                activity={activity}
                sending={sending}
                connection={connection}
                disabled={!jobId || booting}
                jobId={jobId}
                onSend={sendMessage}
                onHide={() => setChatOpenPersisted(false)}
                onOpenSession={openSession}
                onNewSession={startNewSession}
              />
            </div>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setChatOpenPersisted(true)}
            title="Show chat pane"
            aria-label="Show chat pane"
            className="hidden shrink-0 items-center justify-center border-l border-ink-700/80 bg-ink-900/60 px-1.5 text-[10px] font-medium uppercase tracking-wider text-slate-400 transition-colors hover:bg-ink-850 hover:text-cloud lg:flex"
            style={{ writingMode: "vertical-rl" }}
          >
            Chat
          </button>
        )}
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
