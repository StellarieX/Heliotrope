// One place that turns a stored load into the backend's LoadSpec, shared by the
// dashboard and the landing page's live demo so both plan exactly the same way.
//
// Two rules this module exists to enforce:
//   1. Nothing the user did not state is invented. An interruptible load needs
//      its energy (kWh); an atomic load needs its run length. If it is missing the
//      load is reported as "needs detail" and left out of the plan, instead of
//      being planned on a made-up number.
//   2. "+Nh flexible" is real: the latest finish time is `ready by` plus the
//      flexibility, and that is the hard deadline handed to the solver.
//
// The one set of numbers that IS defaulted is the thermal model of a water heater
// or AC (heat loss and gain coefficients) because a person cannot measure them;
// those carry an explicit "estimated" assumption and the comfort band is theirs.

import type { LoadSpec, ThermalSpec } from "../api/types";
import { readyByToDeadline } from "../jobs/normalize";
import type { JobInput } from "../prioritize";

export type StoredJob = JobInput & {
  tempMinC?: number;
  tempMaxC?: number;
  tempTargetC?: number;
};

export type LoadKind = "FIXED" | "THERMAL" | "DEFERRABLE_INTERRUPTIBLE" | "DEFERRABLE_ATOMIC";
export type Detail = "energy" | "duration";

export const THERMAL_RE = /heater|geyser|cool|ac\b|thermal|boiler/i;
export const AC_RE = /cool|ac\b|air/i;
const INTERRUPTIBLE_RE = /\bev\b|charg|pump|motor|borewell|tank|battery/i;

const SLOT_MS = 15 * 60 * 1000;

/** The 15-minute slot boundary at or before `d` (the planner's grid). */
export function floorToSlot(d = new Date()): Date {
  return new Date(Math.floor(d.getTime() / SLOT_MS) * SLOT_MS);
}

/** The next 15-minute slot boundary at or after `d`: a plan must never start in a slot that has already begun. */
export function nextSlot(d = new Date()): Date {
  return new Date(Math.ceil(d.getTime() / SLOT_MS) * SLOT_MS);
}

export function kindOf(j: StoredJob): LoadKind {
  if (j.shiftable === false || j.jobType === "FIXED") return "FIXED";
  // The type stored from the backend's answer (Jev or its rules) always wins over the name.
  if (j.jobType === "THERMAL" || j.jobType === "DEFERRABLE_INTERRUPTIBLE" || j.jobType === "DEFERRABLE_ATOMIC") return j.jobType;
  // Documents written before the type was stored (or saved while the backend was unreachable): infer from the name.
  const text = `${j.name} ${j.kind || ""}`;
  if (THERMAL_RE.test(text)) return "THERMAL";
  return INTERRUPTIBLE_RE.test(text) ? "DEFERRABLE_INTERRUPTIBLE" : "DEFERRABLE_ATOMIC";
}

const positive = (n: number | undefined): n is number => n !== undefined && Number.isFinite(n) && n > 0;

/** Which extra number this load still needs from the user, if any. */
export function detailNeeded(j: StoredJob): Detail | null {
  const k = kindOf(j);
  if (k === "DEFERRABLE_INTERRUPTIBLE" && !positive(j.energyKwh)) return "energy";
  if (k === "DEFERRABLE_ATOMIC" && !positive(j.durationMin)) return "duration";
  return null;
}

/** Latest finish = ready-by (next occurrence) + the flexibility the user allowed. */
export function latestFinish(j: Pick<StoredJob, "readyBy" | "flexHours">, now: Date): Date {
  const base = readyByToDeadline(j.readyBy, now);
  const flex = Number.isFinite(j.flexHours) && j.flexHours > 0 ? j.flexHours : 0;
  return new Date(base.getTime() + flex * 3600 * 1000);
}

function base(j: StoredJob, now: Date, over: Partial<LoadSpec>): LoadSpec {
  const kw = j.powerKw > 0 ? j.powerKw : null;
  return {
    id: j.id,
    normalized_name: j.name,
    category: j.kind || "Load",
    job_type: "FIXED",
    power_kw: kw,
    max_power_kw: kw,
    duration_minutes: null,
    energy_required_kwh: null,
    min_chunk_minutes: null,
    confidence: 1.0,
    ambiguous: false,
    user_input: j.name,
    timezone: "UTC",
    thermal: null,
    explanation: "",
    warnings: [],
    required_fields: [],
    alternatives: [],
    release_at: now.toISOString(),
    deadline_at: latestFinish(j, now).toISOString(),
    assumptions: [],
    ...over,
  };
}

function thermalFor(j: StoredJob, now: Date): LoadSpec {
  const isAc = AC_RE.test(`${j.name} ${j.kind || ""}`);
  const pKw = j.powerKw > 0 ? j.powerKw : isAc ? 1.5 : 2.0;
  const minC = j.tempMinC ?? (isAc ? 22.0 : 40.0);
  const maxC = j.tempMaxC ?? (isAc ? 26.0 : 65.0);
  const tMin = Math.min(minC, maxC);
  const tMax = Math.max(minC, maxC);
  const tInit = isAc ? Math.max(tMax + 2, 30.0) : Math.min(tMin + 5, (tMin + tMax) / 2);
  const tTarget = (tMin + tMax) / 2;
  const coeffs = isAc ? { a: 0.85, b: -1.4, c: 5.1 } : { a: 0.9, b: 2.75, c: 2.0 };
  const thermal: ThermalSpec = {
    ...coeffs,
    max_power_kw: pKw,
    resolution_minutes: 15,
    temperature_initial_c: tInit,
    temperature_min_c: tMin,
    temperature_max_c: tMax,
    temperature_target_c: tTarget,
  };
  const userBand = j.tempMinC !== undefined && j.tempMaxC !== undefined;
  return base(j, now, {
    category: j.kind || (isAc ? "Cooling" : "Water heating"),
    job_type: "THERMAL",
    power_kw: pKw,
    max_power_kw: pKw,
    thermal,
    assumptions: [
      {
        field: "thermal",
        origin: userBand ? "user-configured" : "estimated",
        detail: `comfort band ${tMin}°C–${tMax}°C${userBand ? "" : " (typical range)"}; heat ${isAc ? "gain" : "loss"} coefficients are a typical ${isAc ? "room" : "storage heater"} model, not measured`,
      },
    ],
  });
}

export interface BuiltSpecs {
  specs: LoadSpec[];
  /** Loads left out of the plan because a number the user must give is missing. */
  needsDetail: Array<{ job: StoredJob; need: Detail }>;
}

export function buildSpecs(jobs: StoredJob[], now: Date = nextSlot()): BuiltSpecs {
  const specs: LoadSpec[] = [];
  const needsDetail: BuiltSpecs["needsDetail"] = [];
  for (const j of jobs) {
    const kind = kindOf(j);
    if (kind === "FIXED") {
      specs.push(base(j, now, { job_type: "FIXED", category: j.kind || "Always-on" }));
      continue;
    }
    if (kind === "THERMAL") {
      specs.push(thermalFor(j, now));
      continue;
    }
    const need = detailNeeded(j);
    if (need) {
      needsDetail.push({ job: j, need });
      continue;
    }
    if (kind === "DEFERRABLE_INTERRUPTIBLE") {
      specs.push(
        base(j, now, {
          job_type: "DEFERRABLE_INTERRUPTIBLE",
          category: j.kind || "Flexible",
          energy_required_kwh: j.energyKwh!,
          duration_minutes: positive(j.durationMin) ? Math.max(1, Math.round(j.durationMin)) : null,
          min_chunk_minutes: 15,
          assumptions: [{ field: "energy_required_kwh", origin: "user-configured", detail: `${j.energyKwh} kWh entered by the user` }],
        })
      );
    } else {
      specs.push(
        base(j, now, {
          job_type: "DEFERRABLE_ATOMIC",
          category: j.kind || "Flexible",
          duration_minutes: Math.max(1, Math.round(j.durationMin!)),
          energy_required_kwh: positive(j.energyKwh) ? j.energyKwh : null,
          assumptions: [{ field: "duration_minutes", origin: "user-configured", detail: `${j.durationMin} min entered by the user` }],
        })
      );
    }
  }
  return { specs, needsDetail };
}
