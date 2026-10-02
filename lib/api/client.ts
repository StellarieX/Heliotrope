// Single boundary for all backend calls. No raw fetch() elsewhere.
import type { HealthResponse, NotImplementedError, ScheduleRequest } from "./types";

const BASE = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

async function read<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as NotImplementedError;
    const err = new Error(body.detail || `Backend error ${res.status}`);
    (err as { status?: number }).status = res.status;
    throw err;
  }
  return res.json() as Promise<T>;
}

export async function getHealth(): Promise<HealthResponse> {
  const res = await fetch(`${BASE}/api/v1/health`);
  return read<HealthResponse>(res);
}

/** Phase 1: always rejects with the backend's honest 501 until Phase 4. */
export async function scheduleJobs(body: ScheduleRequest): Promise<never> {
  const res = await fetch(`${BASE}/api/v1/schedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<never>(res);
}

export async function getCarbonSignal(params: {
  start: string;
  end: string;
  resolution_minutes?: number;
  provider?: string;
}): Promise<never> {
  const q = new URLSearchParams({
    start: params.start,
    end: params.end,
    resolution_minutes: String(params.resolution_minutes ?? 15),
    provider: params.provider ?? "synthetic",
  });
  const res = await fetch(`${BASE}/api/v1/carbon?${q}`);
  return read<never>(res);
}
