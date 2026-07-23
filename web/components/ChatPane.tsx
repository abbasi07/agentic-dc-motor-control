"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { ConnectionState } from "@/lib/sse";
import type { ActivityItem, ChatMessage } from "@/lib/types";

export function ChatPane({
  messages,
  activity,
  sending,
  connection,
  disabled,
  onSend,
}: {
  messages: ChatMessage[];
  activity: ActivityItem[];
  sending: boolean;
  connection: ConnectionState;
  disabled: boolean;
  onSend: (text: string) => void;
}) {
  const [tab, setTab] = useState<"chat" | "activity">("chat");
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, activity, tab, sending]);

  const visibleMessages = useMemo(
    () => messages.filter((m) => m.role !== "system"),
    [messages],
  );

  const submit = () => {
    if (!draft.trim() || sending || disabled) return;
    onSend(draft);
    setDraft("");
  };

  return (
    <div className="flex min-h-0 flex-col border-l border-ink-700 bg-ink-900">
      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-ink-700 px-3 py-2">
        <TabButton active={tab === "chat"} onClick={() => setTab("chat")}>
          Conversation
        </TabButton>
        <TabButton active={tab === "activity"} onClick={() => setTab("activity")}>
          Agent activity
          {activity.length > 0 && (
            <span className="ml-1 rounded-full bg-ink-700 px-1.5 text-[10px] text-slate-300">
              {activity.length}
            </span>
          )}
        </TabButton>
      </div>

      {/* Body */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-4">
        {tab === "chat" ? (
          <ChatTranscript messages={visibleMessages} sending={sending} />
        ) : (
          <ActivityFeed activity={activity} />
        )}
      </div>

      {/* Composer */}
      <div className="border-t border-ink-700 p-3">
        <div className="flex items-end gap-2 rounded-lg border border-ink-600 bg-ink-850 px-3 py-2 focus-within:border-accent">
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
                : "Describe your motor, or state the performance you need…"
            }
            className="max-h-40 min-h-[24px] flex-1 resize-none bg-transparent text-sm text-slate-100 outline-none placeholder:text-slate-500 disabled:opacity-50"
          />
          <button
            onClick={submit}
            disabled={disabled || sending || !draft.trim()}
            className="rounded-md bg-accent px-3 py-1.5 text-xs font-semibold text-ink-950 hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {sending ? "…" : "Send"}
          </button>
        </div>
        <p className="mt-1.5 px-1 text-[11px] text-slate-500">
          The copilot computes every number with deterministic tools. It stays locked to
          DC-motor speed-controller design.
        </p>
      </div>
    </div>
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
        "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
        active ? "bg-ink-800 text-slate-100" : "text-slate-400 hover:text-slate-200",
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
      <div className="mx-auto mt-8 max-w-md space-y-4 text-center">
        <div className="text-3xl">⌁</div>
        <h2 className="text-base font-semibold text-slate-200">
          Let&apos;s design a DC-motor speed controller
        </h2>
        <p className="text-sm text-slate-400">
          Start by describing your motor in plain English, or state the performance you
          need. I&apos;ll pin down the motor, requirements, controller type, and design +
          test it — with physics guardrails enforced in the background.
        </p>
        <div className="space-y-2 pt-2 text-left">
          {[
            "I have a 24 V DC motor, about 0.01 kg·m² inertia, reach 1 rad/s.",
            "Settle under 1.5 s with less than 10% overshoot.",
            "Design a PID controller and show me the step response.",
          ].map((s) => (
            <div
              key={s}
              className="rounded-md border border-ink-700 bg-ink-850 px-3 py-2 text-xs text-slate-300"
            >
              “{s}”
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
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={[
          "max-w-[85%] whitespace-pre-wrap rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed",
          isUser
            ? "rounded-br-sm bg-accent text-ink-950"
            : "rounded-bl-sm border border-ink-700 bg-ink-850 text-slate-200",
        ].join(" ")}
      >
        {message.content}
      </div>
    </div>
  );
}

function ThinkingBubble() {
  return (
    <div className="flex justify-start">
      <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-sm border border-ink-700 bg-ink-850 px-4 py-3">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 rounded-full bg-slate-400 animate-pulse-dot"
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
      <div className="mt-8 text-center text-sm text-slate-500">
        Tool calls, design runs, and guardrail events will appear here as the copilot
        works.
      </div>
    );
  }
  const toneColor: Record<ActivityItem["tone"], string> = {
    info: "border-accent/40 bg-accent",
    ok: "border-ok/40 bg-ok",
    warn: "border-warn/40 bg-warn",
    danger: "border-danger/40 bg-danger",
    running: "border-accent/40 bg-accent animate-pulse-dot",
  };
  return (
    <ol className="space-y-2">
      {[...activity].reverse().map((a) => (
        <li
          key={a.id}
          className="flex gap-3 rounded-md border border-ink-700 bg-ink-850 px-3 py-2"
        >
          <span
            className={`mt-1 h-2 w-2 shrink-0 rounded-full ${toneColor[a.tone]}`}
          />
          <div className="min-w-0">
            <p className="text-xs font-medium text-slate-200">{a.title}</p>
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
