// Single boundary for all backend calls. No raw fetch() elsewhere.
import type {
  CarbonSignalResponse,
  ClassifyRequest,
  ClassifyResponse,
  CoordinationResult,
  ExecutionState,
  HealthResponse,
  LoadSpec,
  NotImplementedError,
  ScheduleEventResult,
  ScheduleHistory,
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

/** Real scheduling: ASAP / GREEDY / CPSAT. Returns the backend
 *  SchedulerResult payload (status FEASIBLE/OPTIMAL or honest INFEASIBLE).
 *  Throws with status 400/422/503 on bad input or unavailable providers. */
export async function scheduleJobs(body: ScheduleRequest): Promise<Record<string, unknown>> {
  const res = await fetch(`${BASE}/api/v1/schedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<Record<string, unknown>>(res);
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

export type ForecastMode = "ACTUAL" | "EXPECTED" | "ROBUST";

export interface CarbonForecastPoint {
  timestamp: string;
  predicted_gco2_per_kwh: number;
  lower_gco2_per_kwh: number;
  upper_gco2_per_kwh: number;
}

export interface CarbonForecastResponse {
  signal_type: "FORECAST";
  resolution_minutes: number;
  points: CarbonForecastPoint[];
  provenance: {
    model: string;
    model_description?: string;
    generated_at: string;
    training_window_start: string;
    training_window_end: string;
    training_points: number;
    source_signal: string;
    source_signal_type: string;
    horizon_start: string;
    horizon_end: string;
    resolution_minutes: number;
    interval_nominal_coverage: number;
    uncertainty_method: string;
    configuration?: Record<string, unknown>;
  };
}

export interface ForecastRequestParams {
  start: string;
  end: string;
  resolution_minutes?: number;
  model?: "seasonal" | "persistence" | string;
  lookback_days?: number;
  coverage?: number;
  history_days?: number;
}

/** Phase 5: carbon forecast with empirical prediction interval and provenance. */
export async function getCarbonForecast(
  params: ForecastRequestParams
): Promise<CarbonForecastResponse> {
  const res = await fetch(`${BASE}/api/v1/carbon/forecast`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      start: params.start,
      end: params.end,
      resolution_minutes: params.resolution_minutes ?? 15,
      model: params.model ?? "seasonal",
      lookback_days: params.lookback_days ?? 14,
      coverage: params.coverage ?? 0.9,
      history_days: params.history_days ?? 14,
    }),
  });
  return read<CarbonForecastResponse>(res);
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

/** Phase 7: plan a live schedule from LoadSpecs; versions + state follow. */
export async function planSchedule(body: Record<string, unknown>): Promise<ExecutionState> {
  const res = await fetch(`${BASE}/api/v1/schedules/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<ExecutionState>(res);
}

/** Phase 7: live execution truth for a schedule. */
export async function getScheduleState(scheduleId: string): Promise<ExecutionState> {
  const res = await fetch(`${BASE}/api/v1/schedules/${scheduleId}/state`);
  return read<ExecutionState>(res);
}

/** Phase 7: immutable version history with diffs. */
export async function getScheduleHistory(scheduleId: string): Promise<ScheduleHistory> {
  const res = await fetch(`${BASE}/api/v1/schedules/${scheduleId}/history`);
  return read<ScheduleHistory>(res);
}

/** Phase 7: apply an event; policy may auto-replan. */
export async function postScheduleEvent(
  scheduleId: string,
  body: Record<string, unknown>
): Promise<ScheduleEventResult> {
  const res = await fetch(`${BASE}/api/v1/schedules/${scheduleId}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<ScheduleEventResult>(res);
}

/** Phase 7: manual replan over remaining requirements. */
export async function replanSchedule(
  scheduleId: string,
  body: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const res = await fetch(`${BASE}/api/v1/schedules/${scheduleId}/replan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<Record<string, unknown>>(res);
}

/** Phase 7: deterministic simulated execution. Labeled simulation, never telemetry. */
export async function advanceSimulation(
  scheduleId: string,
  body: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const res = await fetch(`${BASE}/api/v1/simulation/${scheduleId}/advance`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return read<Record<string, unknown>>(res);
}
/** Phase 7: advance wall-clock time for a schedule (polling tick). */
export async function tickSchedule(
  scheduleId: string,
  nowIso: string
): Promise<Record<string, unknown>> {
  const res = await fetch(`${BASE}/api/v1/schedules/${scheduleId}/tick`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ now: nowIso }),
  });
  return read<Record<string, unknown>>(res);
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
