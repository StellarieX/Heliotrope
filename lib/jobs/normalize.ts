// Firestore Job helpers.
//
// NOTE: an earlier version of `normalizeJob` filled in `durationMinutes = 60`
// and derived `energy = power × 1 hour` for every load. That invented physics
// the user never stated, so it was removed. Unknown stays unknown: callers
// build backend LoadSpecs with explicit, labeled assumptions (see the
// dashboard live planner), and the backend feasibility layer reports what is
// still missing instead of guessing it.

export interface FirestoreJob {
  id: string;
  name: string;
  kind: string;
  shiftable?: boolean;
  powerKw: number;
  readyBy: string; // "HH:MM"
  flexHours: number;
}

/** Anchor a "HH:MM" ready-by onto the next occurrence from `now`. */
export function readyByToDeadline(readyBy: string, now = new Date()): Date {
  const [h, m] = readyBy.split(":").map(Number);
  const d = new Date(now);
  d.setHours(h || 0, m || 0, 0, 0);
  if (d <= now) d.setDate(d.getDate() + 1);
  return d;
}
