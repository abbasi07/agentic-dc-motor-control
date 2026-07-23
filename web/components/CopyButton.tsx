"use client";

import { useState } from "react";

import { copyText } from "@/lib/clipboard";

export function CopyButton({
  text,
  label = "Copy",
  title = "Copy section",
  className = "",
}: {
  text: string;
  label?: string;
  title?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  if (!text.trim()) return null;

  const onCopy = async () => {
    await copyText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <button
      type="button"
      onClick={() => void onCopy()}
      title={copied ? "Copied" : title}
      aria-label={copied ? "Copied" : title}
      className={[
        "inline-flex items-center gap-1 rounded-md border border-ink-600/80 bg-ink-900/60 px-1.5 py-0.5 text-[11px] font-medium text-slate-400 transition-colors hover:border-ink-500 hover:text-cloud",
        className,
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
          {label}
        </>
      )}
    </button>
  );
}

function CopyIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden className="block">
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
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden className="block">
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
