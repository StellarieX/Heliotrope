// Single boundary for all backend calls. No raw fetch() elsewhere.
import type {
  CarbonSignalResponse,
  ClassifyRequest,
  ClassifyResponse,
  CoordinationResult,
  ExecutionState,
  HealthResponse,
  LoadSpec,
  PoolStats,
  ScheduleEventResult,
  ScheduleOverrideBody,
  ScheduleOverrideResult,
  ScheduleHistory,
  ScheduleRequest,
  ValidateResponse,
} from "./types";

// NEXT_PUBLIC_BACKEND_URL may carry a trailing slash (e.g. ".../8000/").
// Without trimming, every URL below becomes "...//api/v1/...".
// When NEXT_PUBLIC_BACKEND_URL is unset: local dev talks to the backend directly,
// while a production build uses same-origin relative URLs (/api/v1/...) that
// next.config.ts proxies to BACKEND_URL. Vercel cannot store an empty variable,
// so "unset" must mean same-origin in production rather than localhost.
const DEFAULT_BASE = process.env.NODE_ENV === "production" ? "" : "http://localhost:8000";
const BASE = (process.env.NEXT_PUBLIC_BACKEND_URL ?? DEFAULT_BASE).replace(/\/+$/, "");

const TIMEOUT_MS = 30_000;

function timeoutSignal(): AbortSignal | undefined {
  try {
    if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
      return AbortSignal.timeout(TIMEOUT_MS);
    }
  } catch {
    /* older runtime — proceed without a timeout */
  }
  return undefined;
}

function summarizeDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail || null;
  if (Array.isArray(detail)) {
    // FastAPI RequestValidationError shape: [{ loc, msg, ... }].
    // `detail` is an array here, not a string — join the first few messages
    // instead of rendering "[object Object]".
    const parts = detail
      .slice(0, 3)
      .map((e) => {
        if (typeof e === "string") return e;
        if (e && typeof e === "object") {
          const rec = e as Record<string, unknown>;
          const loc = Array.isArray(rec.loc) ? rec.loc.slice(-2).join(".") : null;
          const msg = typeof rec.msg === "string" ? rec.msg : null;
          if (loc && msg) return `${loc}: ${msg}`;
          if (msg) return msg;
        }
        return null;
      })
      .filter((s): s is string => s !== null && s !== "");
    if (parts.length > 0) {
      const extra = detail.length > 3 ? ` (+${detail.length - 3} more)` : "";
      return parts.join("; ") + extra;
    }
    return null;
  }
  return null;
}

async function toHttpError(res: Response): Promise<Error> {
  let message: string | null = null;
  // The body may be JSON, plain text (proxy/gateway), or empty — never assume.
  const text = await res.text().catch(() => "");
  if (text) {
    try {
      const body = JSON.parse(text) as { detail?: unknown; message?: unknown };
      message =
        summarizeDetail(body.detail) ??
        (typeof body.message === "string" && body.message ? body.message : null);
    } catch {
      // Non-JSON error page (gateway HTML, connection reset text, ...).
      message = text.slice(0, 300);
    }
  }
  const err = new Error(message || `Backend error ${res.status}`);
  (err as { status?: number }).status = res.status;
  return err;
}

async function read<T>(res: Response): Promise<T> {
  if (!res.ok) throw await toHttpError(res);
  const text = await res.text().catch(() => "");
  if (!text) throw new Error(`Backend returned an empty response (${res.status})`);
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`Backend returned non-JSON success (${res.status})`);
  }
}

function requireId(scheduleId: string): string {
  if (!scheduleId) throw new Error("Missing schedule id.");
  return encodeURIComponent(scheduleId);
}

async function postJson<T>(path: string, body: unknown, timeoutMs?: number): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
    signal: timeoutMs ? AbortSignal.timeout(timeoutMs) : timeoutSignal(),
  });
  return read<T>(res);
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    cache: "no-store",
    signal: timeoutSignal(),
  });
  return read<T>(res);
}

export async function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>("/api/v1/health");
}

/**
 * Poll /health until the backend answers. Free hosting tiers put an idle
 * service to sleep and need ~50s to wake it, longer than the 30s request
 * timeout, so the first real call would fail on a healthy deployment. Resolves
 * true once the backend is up, false if it never answered within `maxMs`.
 * `onWaking` fires once, after the first failed attempt.
 */
export async function waitForBackend(opts: {
  maxMs?: number;
  onWaking?: () => void;
  signal?: AbortSignal;
} = {}): Promise<boolean> {
  const { maxMs = 90_000, onWaking, signal } = opts;
  const deadline = Date.now() + maxMs;
  let announced = false;
  while (!signal?.aborted) {
    try {
      const res = await fetch(`${BASE}/api/v1/health`, {
        cache: "no-store",
        signal: AbortSignal.timeout(8_000),
      });
      if (res.ok) return true;
    } catch {
      /* not up yet */
    }
    if (!announced) {
      announced = true;
      onWaking?.();
    }
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, 2_000));
  }
  return false;
}

/** Real scheduling: ASAP / GREEDY / CPSAT. Returns the backend
 *  SchedulerResult payload (status FEASIBLE/OPTIMAL or honest INFEASIBLE).
 *  Throws with status 400/422/503 on bad input or unavailable providers. */
export async function scheduleJobs(body: ScheduleRequest): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>("/api/v1/schedule", body);
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
  });
  // Only an explicit choice is sent; otherwise the backend's configured source is used.
  if (params.provider) q.set("provider", params.provider);
  return getJson<CarbonSignalResponse>(`/api/v1/carbon?${q}`);
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
  return postJson<CarbonForecastResponse>("/api/v1/carbon/forecast", {
    start: params.start,
    end: params.end,
    resolution_minutes: params.resolution_minutes ?? 15,
    model: params.model ?? "seasonal",
    lookback_days: params.lookback_days ?? 14,
    coverage: params.coverage ?? 0.9,
    history_days: params.history_days ?? 14,
  });
}

export interface ScheduleAllocation {
  slot: number;
  timestamp: string;
  power_w: number;
}

export interface CompareSchedulerResult {
  status: string;
  metrics: {
    total_co2_kg: number | null;
    co2_saved_kg: number | null;
    co2_saved_percent: number | null;
    peak_kw: number | null;
    deadline_misses: number;
    /** "FORECAST" when the CO₂ figures come from a predicted signal, "OBSERVED" otherwise. */
    co2_basis?: string;
  };
  schedule: Array<{ job_id: string; name: string; allocations: ScheduleAllocation[] }>;
  explanations: Array<{
    job_id: string;
    name: string;
    reason: string;
    co2_before_kg: number | null;
    co2_after_kg: number | null;
    co2_saved_kg: number | null;
    deadline_preserved: boolean;
  }>;
  signal?: { signal_type?: string };
}

export interface ScheduleCompareResponse {
  reference_scheduler: string;
  results: Record<string, CompareSchedulerResult>;
}

/** Same input through several schedulers (ASAP = "run now" baseline vs CPSAT),
 *  so the impact shown to the user is computed by the backend, not estimated here. */
export async function compareSchedulers(body: Record<string, unknown>): Promise<ScheduleCompareResponse> {
  return postJson<ScheduleCompareResponse>("/api/v1/schedule/compare", body);
}

/** Free text -> classification + canonical LoadSpec + feasibility. Jev decides when the
 *  backend has a key (`provider` = "jev"); otherwise the built-in rules answer
 *  (`provider` = "rule_based"). Typed-as-you-go, so it gives up sooner than a plan. */
export async function classifyLoad(body: ClassifyRequest): Promise<ClassifyResponse> {
  return postJson<ClassifyResponse>("/api/v1/loads/classify", body, 15_000);
}

export interface PoolStatsParams {
  start?: string;
  end?: string;
  resolution_minutes?: number;
}

/** Everyone's planned load: the aggregate of all active schedules on this server.
 *  A 404 means the endpoint isn't deployed yet; callers treat that as "unavailable". */
export async function getPoolStats(params: PoolStatsParams = {}): Promise<PoolStats> {
  const q = new URLSearchParams();
  if (params.start) q.set("start", params.start);
  if (params.end) q.set("end", params.end);
  if (params.resolution_minutes) q.set("resolution_minutes", String(params.resolution_minutes));
  const qs = q.toString();
  return getJson<PoolStats>(`/api/v1/pool/stats${qs ? `?${qs}` : ""}`);
}

export interface PriorityLoadIn {
  id: string;
  name: string;
  kind?: string;
  power_kw: number;
  hours_until_ready: number;
  flex_hours: number;
}

export interface PriorityItem {
  id: string;
  score: number;
  band: "Critical" | "High" | "Normal" | "Low";
  reason: string;
  source: "jev" | "heuristic";
  importance: number | null;
  importance_label: string | null;
  importance_confidence: number | null;
}

export interface PriorityResponse {
  /** "mixed": Jev scored some loads, the rules scored the rest (see notes). */
  provider: "jev" | "mixed" | "heuristic";
  items: PriorityItem[];
  notes: string[];
}

/** Which load matters first. Jev judges how essential each appliance is; the backend
 *  combines that with time pressure, size and rigidity. Labelled `heuristic` when Jev
 *  is not configured or unreachable. */
export async function prioritizeLoads(loads: PriorityLoadIn[]): Promise<PriorityResponse> {
  return postJson<PriorityResponse>("/api/v1/loads/prioritize", { loads });
}

/** Phase 3: a fully specified load -> structured feasibility verdict. */
export async function validateLoad(spec: LoadSpec): Promise<ValidateResponse> {
  return postJson<ValidateResponse>("/api/v1/loads/validate", spec);
}

/** Phase 7: plan a live schedule from LoadSpecs; versions + state follow. */
export async function planSchedule(body: Record<string, unknown>): Promise<ExecutionState> {
  return postJson<ExecutionState>("/api/v1/schedules/plan", body);
}

/** Phase 7: live execution truth for a schedule. */
export async function getScheduleState(scheduleId: string): Promise<ExecutionState> {
  return getJson<ExecutionState>(`/api/v1/schedules/${requireId(scheduleId)}/state`);
}

/** Phase 7: immutable version history with diffs. */
export async function getScheduleHistory(scheduleId: string): Promise<ScheduleHistory> {
  return getJson<ScheduleHistory>(`/api/v1/schedules/${requireId(scheduleId)}/history`);
}

/** Phase 7: apply an event; policy may auto-replan. */
export async function postScheduleEvent(
  scheduleId: string,
  body: Record<string, unknown>
): Promise<ScheduleEventResult> {
  return postJson<ScheduleEventResult>(`/api/v1/schedules/${requireId(scheduleId)}/events`, body);
}

/** User override of one job (run now, pause, cancel, move window). 422 carries the reason. */
export async function overrideSchedule(
  scheduleId: string,
  body: ScheduleOverrideBody
): Promise<ScheduleOverrideResult> {
  return postJson<ScheduleOverrideResult>(`/api/v1/schedules/${requireId(scheduleId)}/override`, body);
}

/** Phase 7: manual replan over remaining requirements. */
export async function replanSchedule(
  scheduleId: string,
  body: Record<string, unknown>
): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>(`/api/v1/schedules/${requireId(scheduleId)}/replan`, body);
}

/** Phase 7: advance wall-clock time for a schedule (polling tick). */
export async function tickSchedule(
  scheduleId: string,
  nowIso: string
): Promise<Record<string, unknown>> {
  return postJson<Record<string, unknown>>(`/api/v1/schedules/${requireId(scheduleId)}/tick`, {
    now: nowIso,
  });
}
/** Phase 6: multi-user coordination. Thin call; the engine lives server-side. */
export async function coordinateBuilding(body: Record<string, unknown>): Promise<CoordinationResult> {
  return postJson<CoordinationResult>("/api/v1/coordination/schedule", body);
}

/** Phase 6: independent vs coordinated on the same input, both real runs. */
export async function compareCoordination(
  body: Record<string, unknown>
): Promise<{ independent: CoordinationResult; coordinated: CoordinationResult }> {
  return postJson<{ independent: CoordinationResult; coordinated: CoordinationResult }>(
    "/api/v1/coordination/compare",
    body
  );
}
