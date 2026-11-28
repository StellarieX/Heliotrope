export function validReadyBy(value: string): boolean {
  return /^(?:[01]?\d|2[0-3]):[0-5]\d$/.test(value);
}

/** Anchor a "HH:MM" ready-by onto the next occurrence from `now`. */
export function readyByToDeadline(readyBy: string, now = new Date()): Date {
  const [h, m] = readyBy.split(":").map(Number);
  const d = new Date(now);
  d.setHours(h || 0, m || 0, 0, 0);
  if (d <= now) d.setDate(d.getDate() + 1);
  return d;
}
