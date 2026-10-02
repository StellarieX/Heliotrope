// Canonical backend contracts (mirror of backend/app/domain).
// The dashboard keeps its local Firestore shape; normalize.ts converts.

export type JobType = "FIXED" | "DEFERRABLE_ATOMIC" | "DEFERRABLE_INTERRUPTIBLE" | "THERMAL";

export interface CanonicalJob {
  id: string;
  name: string;
  type: JobType;
  power_kw: number;
  release_time: string; // ISO, tz-aware
  deadline: string; // ISO, tz-aware
  duration_minutes: number;
  energy_kwh: number;
  flexibility_hours: number;
  interruptible: boolean;
  min_chunk_minutes: number;
}

export interface ScheduleRequest {
  jobs: CanonicalJob[];
  capacity_kw: number;
  scheduler: "ASAP" | "GREEDY" | "CPSAT";
}

export interface NotImplementedError {
  detail: string;
  solver?: string;
  provider?: string;
  jobs_received?: number;
}

export interface HealthResponse {
  status: string;
  service: string;
  env: string;
}
