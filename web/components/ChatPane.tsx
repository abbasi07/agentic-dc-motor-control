"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { ConnectionState } from "@/lib/sse";
import type { ActivityItem, ChatMessage } from "@/lib/types";
import { copyText } from "@/lib/clipboard";

import { SessionHistory } from "./SessionHistory";

export function ChatPane({
  messages,
  activity,
  sending,
  connection,
  disabled,
  jobId,
  onSend,
  onHide,
  onOpenSession,
  onNewSession,
}: {
  messages: ChatMessage[];
  activity: ActivityItem[];
  sending: boolean;
  connection: ConnectionState;
  disabled: boolean;
  jobId: string | null;
  onSend: (text: string) => void;
  onHide?: () => void;
  onOpenSession: (jobId: string) => void;
  onNewSession: () => void;
}) {
  const [tab, setTab] = useState<"chat" | "activity">("chat");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (historyOpen) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, activity, tab, sending, historyOpen]);

  const visibleMessages = useMemo(
    () => messages.filter((m) => m.role !== "system"),
    [messages],
  );
  const conversationStarted = visibleMessages.length > 0;
  const [copiedAll, setCopiedAll] = useState(false);

  const submit = () => {
    if (!draft.trim() || sending || disabled) return;
    onSend(draft);
    setDraft("");
  };

  const copyEntireChat = async () => {
    if (!visibleMessages.length) return;
    const text = visibleMessages
      .map((m) => {
        const who = m.role === "user" ? "User" : "Assistant";
        return `${who}:\n${m.content}`;
      })
      .join("\n\n");
    await copyText(text);
    setCopiedAll(true);
    window.setTimeout(() => setCopiedAll(false), 1200);
  };

  return (
    <div className="flex h-full min-h-0 flex-col border-l border-ink-700/80 bg-ink-900/60">
      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-ink-700/80 px-3 py-2.5">
        {!historyOpen && (
          <>
            <TabButton
              active={tab === "chat"}
              onClick={() => setTab("chat")}
            >
              Conversation
            </TabButton>
            <TabButton
              active={tab === "activity"}
              onClick={() => setTab("activity")}
            >
              Agent activity
              {activity.length > 0 && (
                <span className="ml-1.5 rounded-md bg-violet/20 px-1.5 py-0.5 text-[10px] font-medium text-violet">
                  {activity.length}
                </span>
              )}
            </TabButton>
          </>
        )}
        {historyOpen && (
          <span className="px-1 text-xs font-medium text-cloud/90">History</span>
        )}
        <div className="ml-auto flex items-center gap-0.5">
          {!historyOpen && (
            <button
              type="button"
              onClick={() => void copyEntireChat()}
              disabled={!conversationStarted}
              title={copiedAll ? "Copied" : "Copy entire chat"}
              aria-label={copiedAll ? "Copied" : "Copy entire chat"}
              className={[
                "rounded-lg p-1.5 transition-colors",
                copiedAll
                  ? "bg-ink-850 text-cloud"
                  : "text-slate-400 hover:bg-ink-850 hover:text-cloud/90",
                "disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent",
              ].join(" ")}
            >
              {copiedAll ? <CheckIcon /> : <CopyIcon />}
            </button>
          )}
          <button
            type="button"
            onClick={() => setHistoryOpen((v) => !v)}
            title={historyOpen ? "Back to chat" : "Chat history"}
            aria-label={historyOpen ? "Back to chat" : "Chat history"}
            aria-pressed={historyOpen}
            className={[
              "rounded-lg p-1.5 transition-colors",
              historyOpen
                ? "bg-ink-850 text-cloud"
                : "text-slate-400 hover:bg-ink-850 hover:text-cloud/90",
            ].join(" ")}
          >
            <HistoryIcon />
          </button>
          {onHide && (
            <button
              type="button"
              onClick={onHide}
              title="Hide chat pane"
              aria-label="Hide chat pane"
              className="rounded-lg px-2 py-1.5 text-xs font-medium text-slate-400 transition-colors hover:bg-ink-850 hover:text-cloud/90"
            >
              Hide
            </button>
          )}
        </div>
      </div>

      {/* Body */}
      {historyOpen ? (
        <div className="min-h-0 flex-1">
          <SessionHistory
            currentJobId={jobId}
            onClose={() => setHistoryOpen(false)}
            onNew={() => {
              setHistoryOpen(false);
              onNewSession();
            }}
            onOpen={(id) => {
              setHistoryOpen(false);
              onOpenSession(id);
            }}
            onDeletedCurrent={() => {
              setHistoryOpen(false);
              onNewSession();
            }}
          />
        </div>
      ) : (
        <>
          <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
            {tab === "chat" ? (
              <ChatTranscript messages={visibleMessages} sending={sending} />
            ) : (
              <ActivityFeed activity={activity} />
            )}
          </div>

          {/* Composer */}
          <div className="border-t border-ink-700/80 p-3">
            <div className="flex items-end gap-2 rounded-xl border border-ink-600 bg-ink-850 px-3 py-2.5 transition-shadow focus-within:border-accent/60 focus-within:shadow-glow">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submit();
                  }
                }}
                rows={1}
                disabled={disabled}
                placeholder={
                  disabled
                    ? "Starting a session…"
                    : conversationStarted
                      ? "Type a reply…"
                      : "Describe your motor, or state the performance you need…"
                }
                className="max-h-40 min-h-[24px] flex-1 resize-none bg-transparent text-sm text-cloud outline-none placeholder:text-slate-500 disabled:opacity-50"
              />
              <button
                onClick={submit}
                disabled={disabled || sending || !draft.trim()}
                className="rounded-lg bg-accent px-3.5 py-1.5 text-xs font-semibold text-ink-950 transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {sending ? "Sending" : "Send"}
              </button>
            </div>
            <p className="mt-2 px-1 text-[11px] leading-relaxed text-slate-500">
              Every number is computed with deterministic tools. Scope is locked to DC-motor
              speed-controller design.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

function HistoryIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden
      className="block"
    >
      <path
        d="M8 3.5V8l2.5 1.5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12Z"
        stroke="currentColor"
        strokeWidth="1.4"
      />
    </svg>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={[
        "rounded-lg px-3 py-1.5 text-xs font-medium transition-colors",
        active
          ? "bg-ink-850 text-cloud shadow-panel"
          : "text-slate-400 hover:text-cloud/80",
      ].join(" ")}
    >
      {children}
    </button>
  );
}

function ChatTranscript({
  messages,
  sending,
}: {
  messages: ChatMessage[];
  sending: boolean;
}) {
  if (messages.length === 0 && !sending) {
    return (
      <div className="mx-auto mt-10 max-w-md space-y-5 text-center animate-fade-up">
        <div>
          <p className="mb-1 text-[11px] font-medium uppercase tracking-[0.14em] text-violet">
            Design session
          </p>
          <h2 className="text-lg font-semibold tracking-tight text-cloud">
            DC-motor speed controller
          </h2>
          <p className="mt-2 text-sm leading-relaxed text-slate-400">
            Describe your motor in plain English, or state the performance you need. The
            workspace on the left fills in as motor, requirements, controller, and results
            are locked down.
          </p>
        </div>
        <div className="space-y-2 pt-1 text-left">
          <p className="px-0.5 text-[11px] font-medium uppercase tracking-wider text-slate-500">
            Try something like
          </p>
          {[
            "I have a 24 V DC motor, about 0.01 kg·m² inertia, reach 1 rad/s.",
            "Settle under 1.5 s with less than 10% overshoot.",
            "Design a PID controller and show me the step response.",
          ].map((s) => (
            <div
              key={s}
              className="rounded-lg border border-ink-700/80 bg-ink-850/80 px-3.5 py-2.5 text-xs leading-relaxed text-slate-300"
            >
              {s}
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {messages.map((m, i) => (
        <MessageBubble key={i} message={m} />
      ))}
      {sending && <ThinkingBubble />}
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    const text = message.content ?? "";
    if (!text) return;
    await copyText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div className={`group flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`flex max-w-[85%] flex-col ${isUser ? "items-end" : "items-start"}`}>
        <div
          className={[
            "whitespace-pre-wrap rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed",
            isUser
              ? "rounded-br-md bg-accent text-ink-950"
              : "rounded-bl-md border border-ink-700/80 bg-ink-850 text-cloud/90",
          ].join(" ")}
        >
          {message.content}
        </div>
        <button
          type="button"
          onClick={() => void copy()}
          title={copied ? "Copied" : "Copy message"}
          aria-label={copied ? "Copied" : "Copy message"}
          className={[
            "mt-1 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] transition-opacity",
            isUser
              ? "text-slate-500 hover:text-cloud/80"
              : "text-slate-500 hover:text-cloud/80",
            copied ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus:opacity-100",
          ].join(" ")}
        >
          {copied ? (
            <>
              <CheckIcon />
              Copied
            </>
          ) : (
            <>
              <CopyIcon />
              Copy
            </>
          )}
        </button>
      </div>
    </div>
  );
}

function CopyIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden className="block">
      <rect x="5.5" y="5.5" width="7" height="7" rx="1.2" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M3.5 10.5V3.5a1 1 0 0 1 1-1h7"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden className="block">
      <path
        d="M3.5 8.5 6.5 11.5 12.5 4.5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ThinkingBubble() {
  return (
    <div className="flex justify-start">
      <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-md border border-violet/25 bg-ink-850 px-4 py-3">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 rounded-full bg-violet animate-pulse-dot"
            style={{ animationDelay: `${i * 0.15}s` }}
          />
        ))}
      </div>
    </div>
  );
}

function ActivityFeed({ activity }: { activity: ActivityItem[] }) {
  if (activity.length === 0) {
    return (
      <div className="mt-10 text-center text-sm text-slate-500 animate-fade-up">
        Tool calls, design runs, and guardrail events appear here as the session runs.
      </div>
    );
  }
  const toneColor: Record<ActivityItem["tone"], string> = {
    info: "bg-violet",
    ok: "bg-ok",
    warn: "bg-warn",
    danger: "bg-danger",
    running: "bg-violet animate-pulse-dot",
  };
  return (
    <ol className="space-y-2">
      {[...activity].reverse().map((a) => (
        <li
          key={a.id}
          className="flex gap-3 rounded-xl border border-ink-700/80 bg-ink-850/80 px-3.5 py-2.5"
        >
          <span
            className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${toneColor[a.tone]}`}
          />
          <div className="min-w-0">
            <p className="text-xs font-medium text-cloud/90">{a.title}</p>
            {a.detail && (
              <p className="mt-0.5 break-words font-mono text-[11px] text-slate-400">
                {a.detail}
              </p>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
