// Jev-style classification step: free-text name in, category + shiftability out.
// Only interval loads (power needed for a stretch we can move) are schedulable.
// Always-on loads are filtered out — there is no window to optimize.

export type JobClass = { category: string; shiftable: boolean; why: string };

export function classifyJob(name: string): JobClass {
  const k = name.toLowerCase();
  if (/ev|car|bike|scooter|charger|vehicle|tesla/.test(k))
    return { category: "EV charging", shiftable: true, why: "charges in a movable block" };
  if (/geyser|heater|water|boiler|bath/.test(k))
    return { category: "Water heating", shiftable: true, why: "heats once, stays hot for hours" };
  if (/wash|laundry|dryer|clothes|dishwash/.test(k))
    return { category: "Laundry", shiftable: true, why: "runs a fixed cycle any time before done-by" };
  if (/ac\b|cool|air|pre-?cool/.test(k))
    return { category: "Cooling", shiftable: true, why: "pre-cools ahead of the peak" };
  if (/pump|motor|borewell|tank/.test(k))
    return { category: "Pumping", shiftable: true, why: "fills the tank in one movable run" };
  if (/oven|kiln|furnace|stove|cook|iron/.test(k))
    return { category: "Heating appliance", shiftable: true, why: "runs a fixed cycle that can slide" };
  if (/fridge|refrigerator|freezer/.test(k))
    return { category: "Refrigeration", shiftable: false, why: "must stay powered around the clock" };
  if (/light|fan\b|tv|wifi|router|computer|laptop|projector|fan(?=s)/.test(k))
    return { category: "Always-on", shiftable: false, why: "needed continuously while in use" };
  return { category: "Other", shiftable: true, why: "treated as one movable block — correct us if always-on" };
}

// Back-compat: category only.
export function classifyKind(name: string): string {
  return classifyJob(name).category;
}

// Jev-style decision step: state in, structured decision out.
// Each job's state (deadline, size, flexibility) returns a Score + band + reason.
// Mirrors Jev's Choice/Score pattern; runs locally until wired to the model API.

export type JobInput = {
  id: string;
  name: string;
  kind: string;
  powerKw: number;
  readyBy: string; // "HH:MM"
  flexHours: number;
  shiftable?: boolean;
};

export type RankedJob = JobInput & {
  score: number; // 0–100
  band: "Critical" | "High" | "Normal" | "Low";
  reason: string;
};

function hoursUntilReady(readyBy: string, now = new Date()): number {
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
          : `Ready by ${j.readyBy} (${hrs.toFixed(1)}h out) · ${j.powerKw} kW · +${j.flexHours}h flexible — ${
              band === "Critical" || band === "High" ? "schedule first." : "fits around the big ones."
            }`;
      return { ...j, score, band, reason };
    })
    .sort((a, b) => b.score - a.score);
}
