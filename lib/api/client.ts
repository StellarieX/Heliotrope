// Single boundary for all backend calls. No raw fetch() elsewhere.
import type {
  CarbonSignalResponse,
  ClassifyRequest,
  ClassifyResponse,
  CoordinationResult,
  HealthResponse,
  LoadSpec,
  NotImplementedError,
  ScheduleRequest,
  ValidateResponse,
} from "./types";

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
}): Promise<CarbonSignalResponse> {
  const q = new URLSearchParams({
    start: params.start,
    end: params.end,
    resolution_minutes: String(params.resolution_minutes ?? 15),
    provider: params.provider ?? "synthetic",
  });
  const res = await fetch(`${BASE}/api/v1/carbon?${q}`);
  return read<CarbonSignalResponse>(res);
}

/** Phase 3: free text -> classification + canonical LoadSpec + feasibility.
 *  Runs against the rules-first backend classifier, so it works offline and
 *  returns the same answer for the same input. */
export async function classifyLoad(body: ClassifyRequest): Promise<ClassifyResponse> {
  const res = await fetch(`${BASE}/api/v1/loads/classify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<ClassifyResponse>(res);
}

/** Phase 3: a fully specified load -> structured feasibility verdict. */
export async function validateLoad(spec: LoadSpec): Promise<ValidateResponse> {
  const res = await fetch(`${BASE}/api/v1/loads/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(spec),
  });
  return read<ValidateResponse>(res);
}

/** Phase 6: multi-user coordination. Thin call; the engine lives server-side. */
export async function coordinateBuilding(body: Record<string, unknown>): Promise<CoordinationResult> {
  const res = await fetch(`${BASE}/api/v1/coordination/schedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<CoordinationResult>(res);
}

/** Phase 6: independent vs coordinated on the same input, both real runs. */
export async function compareCoordination(
  body: Record<string, unknown>
): Promise<{ independent: CoordinationResult; coordinated: CoordinationResult }> {
  const res = await fetch(`${BASE}/api/v1/coordination/compare`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<{ independent: CoordinationResult; coordinated: CoordinationResult }>(res);
}
