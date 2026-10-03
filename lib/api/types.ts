// Canonical backend contracts (mirror of backend/app/domain).
// The dashboard keeps its local Firestore shape; normalize.ts converts.

export type JobType = "FIXED" | "DEFERRABLE_ATOMIC" | "DEFERRABLE_INTERRUPTIBLE" | "THERMAL";

// (Removed: the Phase-1 CanonicalJob shape. The backend consumes LoadSpec;
// see below. History kept in git.)

// --- Phase 3: load intelligence -------------------------------------------
// Mirrors backend/app/domain/loads.py and core/feasibility.py.
// `null` always means UNKNOWN, never zero: Heliotrope does not guess physical
// values, and the UI must not render an unknown as a real number.

export type ParameterOrigin =
  | "user-configured"
  | "synthetic default"
  | "estimated"
  | "derived"
  | "legacy-inferred";

export interface Assumption {
  field: string;
  origin: ParameterOrigin;
  detail: string;
}

export type RequiredField =
  | "power_kw"
  | "max_power_kw"
  | "duration_minutes"
  | "energy_required_kwh"
  | "min_chunk_minutes"
  | "release_at"
  | "deadline_at"
  | "temperature_band"
  | "thermal_coefficients"
  | "occupancy_window";

export interface ThermalSpec {
  a: number;
  b: number;
  c: number;
  max_power_kw: number;
  resolution_minutes: number;
  temperature_initial_c: number | null;
  temperature_min_c: number;
  temperature_max_c: number;
  temperature_target_c: number | null;
}

export interface LoadSpec {
  id: string;
  participant_id?: string;
  user_input: string;
  normalized_name: string;
  category: string;
  job_type: JobType;
  confidence: number;
  ambiguous: boolean;
  power_kw: number | null;
  max_power_kw: number | null;
  duration_minutes: number | null;
  energy_required_kwh: number | null;
  min_chunk_minutes: number | null;
  release_at: string | null;
  deadline_at: string | null;
  timezone: string;
  thermal: ThermalSpec | null;
  explanation: string;
  assumptions: Assumption[];
  warnings: string[];
  required_fields: RequiredField[];
  alternatives: string[];
}

export interface ClassificationAlternative {
  category: string;
  job_type: JobType;
  weight: number;
  reason: string;
}

export interface Classification {
  name: string;
  input: string;
  category: string;
  job_type: JobType;
  shiftable: boolean;
  confidence: number;
  ambiguous: boolean;
  reason: string;
  matched_rule: string;
  alternatives: ClassificationAlternative[];
  required_fields: RequiredField[];
  thermal_example: string | null;
  assumptions: Assumption[];
}

export interface FeasibilityIssue {
  code: string;
  severity: "error" | "warning";
  message: string;
  field: string | null;
  detail: Record<string, unknown>;
}

export interface FeasibilityReport {
  feasible: boolean;
  errors: FeasibilityIssue[];
  warnings: FeasibilityIssue[];
  checks_run: string[];
}

export interface ClassifyResponse {
  provider: string;
  classification: Classification;
  confidence: number;
  ambiguous: boolean;
  assumptions: Assumption[];
  normalized_load_spec: LoadSpec;
  feasibility: FeasibilityReport;
}

export interface ValidateResponse {
  feasible: boolean;
  checks_run: string[];
  errors: FeasibilityIssue[];
  warnings: FeasibilityIssue[];
  semantics: {
    load_type: JobType;
    shiftable: boolean;
    decision_variable: "none" | "start" | "power" | "power+state";
    primary_requirement: "baseline_occupancy" | "duration" | "energy" | "state_trajectory";
    constraints: string[];
    notes: string;
  };
  explanation: string;
  summary: string;
  metric_inputs: Record<string, unknown>;
}

export interface ClassifyRequest {
  name: string;
  power_kw?: number;
  max_power_kw?: number;
  duration_minutes?: number;
  energy_required_kwh?: number;
  min_chunk_minutes?: number;
  release_wall?: string;
  deadline_wall?: string;
  timezone?: string;
  job_type?: JobType;
}

export interface ScheduleRequest {
  jobs: LoadSpec[];
  capacity_kw: number;
  scheduler: "ASAP" | "GREEDY" | "CPSAT";
  objective?: Record<string, number>;
  solver_config?: Record<string, number | string>;
  carbon_provider?: string;
  explain?: boolean;
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

export interface ExecutionJobState {
  job_id: string;
  participant_id: string;
  status: string;
  scheduled_start: string | null;
  scheduled_end: string | null;
  energy_delivered_kwh: number;
  expected_energy_kwh: number;
  note: string;
}

export interface ExecutionState {
  schedule_id: string;
  lifecycle: string;
  version: number;
  solver_status: string;
  jobs: ExecutionJobState[];
}

export interface ScheduleHistory {  schedule_id: string;
  lifecycle: string;
  versions: Array<{
    version: number;
    created_at: string;
    reason: string;
    solver_status: string;
    carbon_estimate_kg: number | null;
    peak_kw: number | null;
    changed_jobs: Array<{
      job_id: string;
      previous_start: string | null;
      new_start: string | null;
      previous_end: string | null;
      new_end: string | null;
      change_minutes: number;
      reason: string;
    }>;
  }>;
}

export interface ScheduleEventResult {
  state: ExecutionState;
  replan_advised: boolean;
  notes: string[];
  [key: string]: unknown;
}

export interface CoordinationAggregatePoint {
  timestamp: string;
  baseline_kw: number;
  flexible_kw: number;
  total_kw: number;
  capacity_kw: number;
  utilization: number;
  carbon_intensity: number;
  congestion_score: number;
}

export interface CoordinationCongestionPoint {
  timestamp: string;
  aggregate_load: number;
  capacity_kw: number;
  utilization: number;
  congestion_score: number;
}

export interface CoordinationResult {
  status: string;
  coordination_mode: "INDEPENDENT" | "COORDINATED";
  participants: Array<{
    participant_id: string;
    inconvenience_score: number;
    delay_minutes: number;
    jobs_shifted: number;
    job_count: number;
    co2_kg: number;
  }>;
  jobs: Array<{
    participant_id: string;
    job_id: string;
    name: string;
    scheduled_start: string;
    scheduled_end: string;
    energy_kwh: number;
    power_kw: number;
    delay_minutes: number;
    carbon_kg: number;
    reason: string;
  }>;
  aggregate_profile: CoordinationAggregatePoint[];
  congestion_profile: CoordinationCongestionPoint[];
  metrics: {
    total_energy_kwh: number | null;
    total_co2_kg: number | null;
    peak_kw: number | null;
    capacity_violations: number;
    total_delay_minutes: number;
    worst_inconvenience: number;
    participant_count: number;
    job_count: number;
    solve_time_ms: number | null;
  };
  fairness_mode: string;
  solver_status: string;
  reason: string;
  signal_provenance: Record<string, unknown>;
}

export type SignalType = "MARGINAL" | "AVERAGE" | "PROXY" | "SYNTHETIC";

export interface CarbonPointOut {
  timestamp: string;
  carbon_intensity_gco2_per_kwh: number;
}

export interface CarbonSignalResponse {
  start: string;
  end: string;
  resolution_minutes: number;
  signal_type: SignalType;
  source: string;
  points: CarbonPointOut[];
  quality: {
    complete: boolean;
    missing_points: number;
    interpolated_points: number;
    source: string;
    signal_type: SignalType;
    is_forecast: boolean;
  };
}
