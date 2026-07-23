"use client";

// SSE subscription hook for GET /jobs/{id}/events.
//
// The browser's native EventSource cannot send an Authorization header, so we stream over
// fetch (via @microsoft/fetch-event-source) which lets us attach the Bearer key. Events
// carry a fixed enum of types (saas/events.py); the caller switches on `type`.

import { fetchEventSource } from "@microsoft/fetch-event-source";
import { useEffect, useRef, useState } from "react";

import { authHeaders, eventsUrl } from "./api";
import type { CopilotEvent, EventType } from "./types";

export type ConnectionState = "idle" | "connecting" | "open" | "closed" | "error";

interface UseEventStreamOptions {
  onEvent: (event: CopilotEvent) => void;
  enabled?: boolean;
}

/**
 * Subscribe to a job's live event stream. Reconnects automatically and tears down when
 * the job changes or the component unmounts.
 */
export function useEventStream(
  jobId: string | null,
  { onEvent, enabled = true }: UseEventStreamOptions,
): ConnectionState {
  const [state, setState] = useState<ConnectionState>("idle");
  // Keep the latest callback without resubscribing on every render.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!jobId || !enabled) {
      setState("idle");
      return;
    }
    const controller = new AbortController();
    setState("connecting");

    fetchEventSource(eventsUrl(jobId), {
      signal: controller.signal,
      headers: authHeaders(),
      openWhenHidden: true,
      onopen: async (res) => {
        if (res.ok) {
          setState("open");
        } else {
          setState("error");
          throw new Error(`SSE open failed: ${res.status}`);
        }
      },
      onmessage: (msg) => {
        if (!msg.data) return;
        try {
          const parsed = JSON.parse(msg.data) as CopilotEvent;
          // Prefer the SSE `event:` name, falling back to the payload type.
          const type = (msg.event || parsed.type) as EventType;
          onEventRef.current({ ...parsed, type });
        } catch {
          /* ignore malformed frames */
        }
      },
      onclose: () => {
        setState("closed");
      },
      onerror: (err) => {
        setState("error");
        // Returning a number here would set the retry interval; let the library use its
        // default exponential backoff. Do NOT throw (that would stop retrying).
        return undefined;
      },
    }).catch(() => {
      setState("closed");
    });

    return () => {
      controller.abort();
      setState("closed");
    };
  }, [jobId, enabled]);

  return state;
}
