import { doc, getDoc, serverTimestamp, setDoc } from "firebase/firestore";
import { getDb } from "./firebase";

// What we know about the person's building, turned into starting points.
//
// Onboarding asks "what do you do" and "where will this run". This module is
// where those two answers change what the app offers: which appliances are shown
// first, a starting value for "Max power at once (kW)", and one sentence of advice.
//
// Every number here is a typical starting value for that kind of appliance, not a
// measurement of the user's own equipment. The user sees each one in an editable
// field and is told to check the appliance label.

export const OCCUPATIONS = ["Student", "Hostel staff", "Homeowner", "Facility manager", "Researcher", "Other"] as const;
export const PLACES = ["Hostel block", "Home", "Campus", "Other"] as const;

export type Occupation = (typeof OCCUPATIONS)[number];
export type Place = (typeof PLACES)[number];

export interface LoadPreset {
  id: string;
  label: string;
  /** What goes in the load's name field; the backend classifies this text. */
  name: string;
  powerKw: number;
  /** "Needed by" time, HH:MM. */
  readyBy: string;
  /** "Can finish up to N h late". */
  flexHours: number;
  /** Total energy per run, for loads that can pause and resume. */
  energyKwh?: number;
  /** Length of one run in minutes, for loads that run in one go. */
  durationMin?: number;
}

const P = (p: LoadPreset): LoadPreset => p;

/** Every preset, in the order "More loads" lists the ones not already shown first. */
export const PRESETS: LoadPreset[] = [
  // Students (a room, a few outlets)
  P({ id: "device-charging", label: "Phone charging", name: "Phone charging", powerKw: 0.1, readyBy: "08:00", flexHours: 4, energyKwh: 0.2, durationMin: 120 }),
  P({ id: "escooter", label: "E-scooter charging", name: "E-scooter charging", powerKw: 0.25, readyBy: "08:00", flexHours: 3, energyKwh: 0.5, durationMin: 120 }),
  P({ id: "induction", label: "Kettle or induction cooktop", name: "Induction cooktop", powerKw: 1.4, readyBy: "19:30", flexHours: 1, durationMin: 30 }),
  P({ id: "room-heater", label: "Room heater", name: "Room heater", powerKw: 1, readyBy: "06:30", flexHours: 1 }),
  P({ id: "room-cooler", label: "Room cooler", name: "Room cooler", powerKw: 0.3, readyBy: "21:00", flexHours: 1 }),

  // Hostel staff (shared services)
  P({ id: "borewell-pump", label: "Water pump or borewell", name: "Borewell pump", powerKw: 1.5, readyBy: "08:00", flexHours: 3, energyKwh: 4 }),
  P({ id: "hostel-geysers", label: "Geyser bank", name: "Geyser bank", powerKw: 6, readyBy: "06:00", flexHours: 1 }),
  P({ id: "hostel-laundry", label: "Laundry machines", name: "Laundry machines", powerKw: 3, readyBy: "17:00", flexHours: 4, durationMin: 90 }),
  P({ id: "corridor-cooler", label: "Corridor water cooler", name: "Corridor water cooler", powerKw: 0.4, readyBy: "08:00", flexHours: 2 }),

  // Homes
  P({ id: "ev-charger", label: "EV charger", name: "EV charger", powerKw: 7.4, readyBy: "07:00", flexHours: 3, energyKwh: 20 }),
  P({ id: "water-heater", label: "Water heater", name: "Water heater", powerKw: 2, readyBy: "06:00", flexHours: 2 }),
  P({ id: "washing-machine", label: "Washing machine", name: "Washing machine", powerKw: 2, readyBy: "18:00", flexHours: 4, durationMin: 60 }),
  P({ id: "dishwasher", label: "Dishwasher", name: "Dishwasher", powerKw: 1.8, readyBy: "07:00", flexHours: 5, durationMin: 90 }),
  P({ id: "ac-precool", label: "AC pre-cool", name: "AC pre-cool", powerKw: 1.5, readyBy: "18:00", flexHours: 1 }),

  // Facilities and campuses
  P({ id: "hvac-precool", label: "HVAC pre-cooling", name: "HVAC pre-cooling", powerKw: 15, readyBy: "08:00", flexHours: 2 }),
  P({ id: "water-pumping", label: "Water pumping", name: "Water pump", powerKw: 5.5, readyBy: "09:00", flexHours: 3, energyKwh: 30 }),
  P({ id: "ev-fleet", label: "EV fleet charging", name: "EV fleet charging", powerKw: 22, readyBy: "06:30", flexHours: 3, energyKwh: 80 }),
  P({ id: "chiller", label: "Chiller", name: "Chiller", powerKw: 20, readyBy: "09:00", flexHours: 2 }),
  P({ id: "campus-geysers", label: "Geyser bank (campus)", name: "Campus geyser bank", powerKw: 12, readyBy: "06:00", flexHours: 1 }),
  P({ id: "campus-laundry", label: "Laundry", name: "Campus laundry", powerKw: 6, readyBy: "16:00", flexHours: 4, durationMin: 120 }),

  // Labs
  P({ id: "lab-batch", label: "Compute batch job", name: "Compute batch job", powerKw: 3, readyBy: "08:00", flexHours: 4, durationMin: 240 }),
  P({ id: "lab-equipment", label: "Lab equipment run", name: "Lab equipment run", powerKw: 2.5, readyBy: "09:00", flexHours: 3, durationMin: 120 }),
  P({ id: "sample-freezer", label: "Sample freezer (always on)", name: "Sample freezer", powerKw: 0.4, readyBy: "00:00", flexHours: 0 }),
];

const BY_ID = new Map(PRESETS.map((p) => [p.id, p]));
const pick = (ids: string[]) => ids.flatMap((id) => (BY_ID.has(id) ? [BY_ID.get(id)!] : []));

const FOR_OCCUPATION: Record<string, string[]> = {
  Student: ["device-charging", "escooter", "induction", "room-heater", "room-cooler"],
  "Hostel staff": ["borewell-pump", "hostel-geysers", "hostel-laundry", "corridor-cooler"],
  Homeowner: ["ev-charger", "water-heater", "washing-machine", "dishwasher", "ac-precool"],
  "Facility manager": ["hvac-precool", "water-pumping", "ev-fleet", "chiller", "campus-geysers", "campus-laundry"],
  Researcher: ["lab-batch", "lab-equipment", "sample-freezer", "ev-charger"],
  Other: ["ev-charger", "water-heater", "washing-machine", "borewell-pump"],
};

/** Loads that are common in a place whatever the person does there. They lead "More loads". */
const FOR_PLACE: Record<string, string[]> = {
  "Hostel block": ["hostel-geysers", "borewell-pump", "hostel-laundry"],
  Home: ["washing-machine", "water-heater", "ev-charger"],
  Campus: ["water-pumping", "campus-laundry", "ev-fleet"],
  Other: [],
};

/** Typical "Max power at once" for the kind of site the person works in. */
const CAPACITY_KW: Record<string, number> = {
  Student: 3,
  "Hostel staff": 25,
  Homeowner: 10,
  "Facility manager": 100,
  Researcher: 30,
  Other: 10,
};

const SITE: Record<string, string> = {
  Student: "a student room",
  "Hostel staff": "a hostel block",
  Homeowner: "a home",
  "Facility manager": "a large facility",
  Researcher: "a lab building",
  Other: "a small site",
};

const ADVICE: Record<string, string> = {
  Student: "Chargers and heaters are the easy wins: they can wait a few hours without you noticing, so leave cooking out unless the time is truly flexible.",
  "Hostel staff": "Pumps, geysers and laundry are shared by many people and run for hours, so letting them finish a little late moves the most energy.",
  Homeowner: "Start with the EV, the water heater and the washing machine: they are big, and each only has to be ready by a certain time.",
  "Facility manager": "Pre-cooling, pumping and fleet charging are large and predictable; give each a realistic late-finish allowance and keep one limit for the whole site.",
  Researcher: "Batch jobs and long equipment runs can usually start later; list the freezer as always on so the plan counts its power but never moves it.",
  Other: "Add the biggest loads that only need to be ready by a certain time; you can refine the rest from the dashboard.",
};

export interface OccupationProfile {
  /** Shown first in step 3. */
  primary: LoadPreset[];
  /** Everything else, behind "More loads". */
  more: LoadPreset[];
  /** Suggested "Max power at once (kW)". */
  capacityKw: number;
  /** One line explaining where the suggestion comes from. */
  capacityNote: string;
  advice: string;
}

function niceCapacity(kw: number): number {
  if (kw >= 100) return Math.round(kw / 10) * 10;
  if (kw >= 20) return Math.round(kw / 5) * 5;
  return Math.round(kw);
}

export function profileFor(occupation: string, place: string, rooms?: number | null): OccupationProfile {
  const key = occupation in FOR_OCCUPATION ? occupation : "Other";
  const primary = pick(FOR_OCCUPATION[key]);
  const shown = new Set(primary.map((p) => p.id));
  const placeFirst = pick(FOR_PLACE[place] ?? []).filter((p) => !shown.has(p.id));
  const placeIds = new Set(placeFirst.map((p) => p.id));
  const rest = PRESETS.filter((p) => !shown.has(p.id) && !placeIds.has(p.id));

  let capacity = CAPACITY_KW[key];
  let note = `A typical starting point for ${SITE[key]}. Set it to your sanctioned load or breaker rating.`;
  if (place === "Home" && capacity > 10) {
    capacity = 10;
    note = "A typical home connection is about 5 to 10 kW. Set it to your sanctioned load or breaker rating.";
  }
  if (rooms && rooms > 0 && (key === "Hostel staff" || key === "Facility manager")) {
    const scaled = niceCapacity(Math.max(capacity, rooms * 0.5));
    if (scaled !== capacity) {
      capacity = scaled;
      note = `A rough guess of 0.5 kW per room for shared loads (${rooms} rooms). Set it to your sanctioned load or breaker rating.`;
    }
  }
  return { primary, more: [...placeFirst, ...rest], capacityKw: capacity, capacityNote: note, advice: ADVICE[key] };
}

// "Max power at once" is private (it describes the site), so it never goes in the
// world-readable profile. It lives in the owner-only `users/{uid}/settings/prefs`
// document, with this browser's copy as a fast cache for the first render.
const capacityKey = (uid: string) => `heliotrope.maxPowerKw.${uid}`;

function cacheMaxPower(uid: string, kw: number): void {
  try {
    localStorage.setItem(capacityKey(uid), String(kw));
  } catch {
    /* storage unavailable: the account copy still holds it */
  }
}

export function saveMaxPower(uid: string, kw: number): void {
  cacheMaxPower(uid, kw);
  const db = getDb();
  if (!db) return;
  void setDoc(
    doc(db, "users", uid, "settings", "prefs"),
    { maxPowerKw: kw, updatedAt: serverTimestamp() },
    { merge: true }
  ).catch(() => {
    /* offline or rules: the browser copy is still used */
  });
}

/** The saved value from the account (any device), refreshing the browser cache. */
export async function loadMaxPower(uid: string): Promise<number | null> {
  const db = getDb();
  if (!db) return readMaxPower(uid);
  try {
    const snap = await getDoc(doc(db, "users", uid, "settings", "prefs"));
    const kw = snap.exists() ? Number(snap.data().maxPowerKw) : NaN;
    if (Number.isFinite(kw) && kw > 0) {
      cacheMaxPower(uid, kw);
      return kw;
    }
  } catch {
    /* fall back to the browser copy */
  }
  return readMaxPower(uid);
}

export function readMaxPower(uid: string): number | null {
  try {
    const n = Number(localStorage.getItem(capacityKey(uid)));
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}
