// Local classification hint: free-text name in, category + shiftability out.
// Only interval loads (power needed for a stretch we can move) are schedulable.
// Always-on loads are filtered out — there is no window to optimize.
//
// ARCHITECTURAL NOTE. This local `classifyJob` is a UI hint only.
// The three layers below are DIFFERENT things and are kept separate:
//
//   1. CLASSIFICATION  what kind of load is this? -> backend /api/v1/loads/classify
//   2. PRIORITIZATION  which one matters first?  -> jevRank(), below
//   3. OPTIMIZATION   when should it actually run? -> backend /api/v1/schedule
//
// jevRank is a heuristic display order, NOT the physical scheduler. It is kept
// as-is for back-compat. Classification now lives in the backend; the two may
// disagree, and when they do the backend wins.

export type JobClass = { category: string; shiftable: boolean; why: string };

// Whole-word matching only: "ev" must not fire inside "level", "ac" inside "lilac",
// "iron" inside "environment". Plurals are listed where they matter.
const w = (...words: string[]) => new RegExp("\\b(?:" + words.join("|") + ")\\b");

const DEVICE_RE = w("laptops?", "phones?", "mobiles?", "tablets?", "power ?banks?");
const CHARGE_RE = w("charging", "charger", "chargers", "charge");
const EV_RE = w("evs?", "e-?bikes?", "e-?scooters?", "scooters?", "bikes?", "cars?", "vehicles?", "tesla", "chargers?");
const SPACE_HEAT_RE = w("room heaters?", "space heaters?", "blowers?", "fan heaters?");
const WATER_HEAT_RE = w("geysers?", "water heaters?", "heaters?", "boilers?", "baths?", "hot water", "immersion");
const LAUNDRY_RE = w("wash(?:ing)?", "washers?", "laundry", "dryers?", "clothes", "dishwash(?:er|ing)?");
const COOLING_RE = w("acs?", "coolers?", "cooling", "air[- ]?con(?:ditioner|ditioning)?", "pre-?cool(?:ing)?");
const PUMP_RE = w("pumps?", "motors?", "borewells?", "tanks?");
const HEATING_APPLIANCE_RE = w("ovens?", "kilns?", "furnaces?", "stoves?", "cook(?:ing|er|top)?", "cooktops?", "kettles?", "induction", "irons?");
const REFRIGERATION_RE = w("fridges?", "refrigerators?", "freezers?");
const ALWAYS_ON_RE = w("lights?", "lighting", "fans?", "tvs?", "wifi", "routers?", "computers?", "laptops?", "projectors?");

export function classifyJob(name: string): JobClass {
  const k = name.toLowerCase();
  if (DEVICE_RE.test(k) && CHARGE_RE.test(k))
    return { category: "Device charging", shiftable: true, why: "charges in a movable block" };
  if (EV_RE.test(k))
    return { category: "EV charging", shiftable: true, why: "charges in a movable block" };
  if (SPACE_HEAT_RE.test(k))
    return { category: "Space heating", shiftable: true, why: "pre-heats ahead of when it is needed" };
  if (WATER_HEAT_RE.test(k))
    return { category: "Water heating", shiftable: true, why: "heats once, stays hot for hours" };
  if (LAUNDRY_RE.test(k))
    return { category: "Laundry", shiftable: true, why: "runs a fixed cycle any time before it is needed" };
  if (REFRIGERATION_RE.test(k))
    return { category: "Refrigeration", shiftable: false, why: "must stay powered around the clock" };
  if (COOLING_RE.test(k))
    return { category: "Cooling", shiftable: true, why: "pre-cools ahead of the peak" };
  if (PUMP_RE.test(k))
    return { category: "Pumping", shiftable: true, why: "fills the tank in one movable run" };
  if (HEATING_APPLIANCE_RE.test(k))
    return { category: "Heating appliance", shiftable: true, why: "runs a fixed cycle that can slide" };
  if (ALWAYS_ON_RE.test(k))
    return { category: "Always-on", shiftable: false, why: "needed continuously while in use" };
  return { category: "Other", shiftable: true, why: "treated as one movable block — correct us if always-on" };
}

// Back-compat: category only.
export function classifyKind(name: string): string {
  return classifyJob(name).category;
}

import type { JobType } from "./api/types";

// Local scoring heuristic: state in, structured Score + band + reason out.
// This is PRIORITIZATION, not optimization — see the note at the top of the file.
// It never calls any model API; all numbers come from the job's own fields.

export type JobInput = {
  id: string;
  name: string;
  kind: string;
  powerKw: number;
  readyBy: string; // "HH:MM"
  flexHours: number;
  shiftable?: boolean;
  // Phase 3 additions. Optional because every document written before Phase 3
  // lacks them; `normalize_firestore_job` treats absent as UNKNOWN, not zero.
  // Nothing below reads these — jevRank's behaviour is unchanged.
  jobType?: JobType;
  energyKwh?: number;
  durationMin?: number;
  maxPowerKw?: number;
  minChunkMin?: number;
  confidence?: number;
};

export type RankedJob = JobInput & {
  score: number; // 0–100
  band: "Critical" | "High" | "Normal" | "Low";
  reason: string;
  /** Who produced this score: "jev" (Jev judged importance), "heuristic" (backend,
   *  no Jev), or "local" (this file, when the backend was unreachable). */
  source?: "jev" | "heuristic" | "local";
};

export function hoursUntilReady(readyBy: string, now = new Date()): number {
  const [h, m] = readyBy.split(":").map(Number);
  if (Number.isNaN(h)) return 12;
  const target = h + (m || 0) / 60;
  const cur = now.getHours() + now.getMinutes() / 60;
  return (target - cur + 24) % 24;
}

export function jevRank(jobs: JobInput[], now = new Date()): RankedJob[] {
  return jobs
    .filter((j) => j.shiftable !== false)
    .map((j) => {
      const hrs = hoursUntilReady(j.readyBy, now);
      const urgency = Math.max(0, 1 - hrs / 24);
      const size = Math.min(Math.max(j.powerKw / 7.5, 0), 1);
      const rigidity = 1 - Math.min(Math.max(j.flexHours / 6, 0), 1);
      const score = Math.round(100 * (0.45 * urgency + 0.3 * size + 0.25 * rigidity));
      const band: RankedJob["band"] = score >= 70 ? "Critical" : score >= 50 ? "High" : score >= 30 ? "Normal" : "Low";
      const reason =
        hrs < 0.1
          ? "Needed right now — lock it first."
          : `Needed by ${j.readyBy} (${hrs.toFixed(1)} h away) · ${j.powerKw} kW · can finish up to ${j.flexHours} h late — ${
              band === "Critical" || band === "High" ? "schedule first." : "fits around the big ones."
            }`;
      return { ...j, score, band, reason, source: "local" as const };
    })
    .sort((a, b) => b.score - a.score);
}
