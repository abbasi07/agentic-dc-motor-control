// Typed API client for the Control Design Copilot backend (FastAPI).
//
// Every /jobs request carries `Authorization: Bearer <key>` (E2.5 auth). The base URL +
// key are browser-visible config (NEXT_PUBLIC_*); the dev bootstrap key is used out of
// the box, real per-user accounts come later.

import type { JobPublic, PlantPublic, Workspace } from "./types";

export const API_BASE: string =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") || "http://localhost:8000";

const DEFAULT_KEY: string = process.env.NEXT_PUBLIC_COPILOT_API_KEY || "dev-local-key";

// The API key is kept in module state so the whole app shares one value; it can be
// overridden at runtime from the UI (settings field) without a rebuild.
let apiKey: string = DEFAULT_KEY;

export function setApiKey(key: string): void {
  apiKey = key.trim();
  if (typeof window !== "undefined") {
    window.localStorage.setItem("copilot_api_key", apiKey);
  }
}

export function getApiKey(): string {
  if (typeof window !== "undefined") {
    const stored = window.localStorage.getItem("copilot_api_key");
    if (stored) apiKey = stored;
  }
  return apiKey;
}

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return {
    Authorization: `Bearer ${getApiKey()}`,
    ...(extra || {}),
  };
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...(init?.headers || {}),
      },
    });
  } catch (e) {
    throw new ApiError(0, `Network error: cannot reach the API at ${API_BASE}.`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// --------------------------------------------------------------------------- //
// Endpoints
// --------------------------------------------------------------------------- //
export function health(): Promise<{ status: string; scope: string }> {
  return request("/health");
}

export function listPlants(): Promise<{ plants: PlantPublic[] }> {
  return request("/plants");
}

export function createJob(
  plant_id = "dc_motor_ctms",
  mode: "script" | "heuristic" | "llm" = "heuristic",
): Promise<JobPublic> {
  return request("/jobs", {
    method: "POST",
    body: JSON.stringify({ plant_id, mode }),
  });
}

export function listJobs(): Promise<{ jobs: JobPublic[] }> {
  return request("/jobs");
}

export function getJob(jobId: string): Promise<JobPublic> {
  return request(`/jobs/${jobId}`);
}

export function deleteJob(jobId: string): Promise<{ deleted: boolean; job_id: string }> {
  return request(`/jobs/${jobId}`, { method: "DELETE" });
}

export function getWorkspace(jobId: string): Promise<Workspace> {
  return request(`/jobs/${jobId}/workspace`);
}

// Chat-first Design Agent (OpenAI-only). Drives the deterministic engine via tools.
export function agentChat(jobId: string, message: string): Promise<JobPublic> {
  return request(`/jobs/${jobId}/agent`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export function runDesign(jobId: string): Promise<JobPublic> {
  return request(`/jobs/${jobId}/run`, { method: "POST", body: JSON.stringify({}) });
}

export function getStatus(jobId: string): Promise<Record<string, unknown>> {
  return request(`/jobs/${jobId}/status`);
}

export function exportJob(
  jobId: string,
): Promise<{ export_path: string; job: JobPublic }> {
  return request(`/jobs/${jobId}/export`, { method: "POST" });
}

// The download endpoint streams a file; browsers cannot set an Authorization header on a
// plain link, so fetch it with the header and hand back an object URL.
export async function downloadExport(jobId: string): Promise<{ url: string; filename: string }> {
  const res = await fetch(`${API_BASE}/jobs/${jobId}/export/download`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new ApiError(res.status, `Download failed (${res.status}).`);
  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") || "";
  const match = cd.match(/filename="?([^"]+)"?/);
  const filename = match ? match[1] : `controller_package_${jobId}.zip`;
  return { url: URL.createObjectURL(blob), filename };
}

export function eventsUrl(jobId: string): string {
  return `${API_BASE}/jobs/${jobId}/events`;
}
