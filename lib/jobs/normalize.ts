// Firestore Job -> canonical backend Job. Backwards compatible:
// existing fields are preserved, never deleted; the backend gains the
// richer semantics (release/deadline datetimes, duration, type) it needs.
import type { CanonicalJob, JobType } from "../api/types";

export interface FirestoreJob {
  id: string;
  name: string;
  kind: string;
  shiftable?: boolean;
  powerKw: number;
  readyBy: string; // "HH:MM"
  flexHours: number;
}

function inferType(kind: string, shiftable?: boolean): JobType {
  if (shiftable === false) return "FIXED";
  if (/heater|geyser|cool|ac\b|thermal/i.test(kind)) return "THERMAL";
  if (/laundry|wash|ev|charge|pump/i.test(kind)) return "DEFERRABLE_INTERRUPTIBLE";
  return "DEFERRABLE_ATOMIC";
}

/** Anchor a "HH:MM" ready-by onto the next occurrence from `now`. */
export function readyByToDeadline(readyBy: string, now = new Date()): Date {
  const [h, m] = readyBy.split(":").map(Number);
  const d = new Date(now);
  d.setHours(h || 0, m || 0, 0, 0);
  if (d <= now) d.setDate(d.getDate() + 1);
  return d;
}

export function normalizeJob(f: FirestoreJob, now = new Date()): CanonicalJob {
  const deadline = readyByToDeadline(f.readyBy, now);
  const durationMinutes = 60; // default 1h block until the user declares durations
  return {
    id: f.id,
    name: f.name,
    type: inferType(f.kind, f.shiftable),
    power_kw: f.powerKw,
    release_time: now.toISOString(),
    deadline: deadline.toISOString(),
    duration_minutes: durationMinutes,
    energy_kwh: (f.powerKw * durationMinutes) / 60,
    flexibility_hours: f.flexHours,
    interruptible: f.kind === "Laundry",
    min_chunk_minutes: 15,
  };
}
