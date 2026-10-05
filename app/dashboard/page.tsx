"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { onAuthStateChanged, signInWithPopup, signOut, type User } from "firebase/auth";
import { addDoc, collection, deleteDoc, doc, getDoc, getDocs } from "firebase/firestore";
import { getDb, getFirebaseAuth, getGoogleProvider } from "../../lib/firebase";
import { jevRank, classifyJob, type JobInput, type RankedJob } from "../../lib/prioritize";
import {
  advanceSimulation,
  classifyLoad,
  coordinateBuilding,
  getCarbonForecast,
  getCarbonSignal,
  getScheduleHistory,
  getScheduleState,
  planSchedule,
  postScheduleEvent,
  replanSchedule,
  tickSchedule,
  validateLoad,
  type CarbonForecastResponse,
  type ForecastMode,
} from "../../lib/api/client";
import type {
  CarbonSignalResponse,
  CoordinationResult,
  ExecutionState,
  JobType,
  LoadSpec,
  ScheduleHistory,
  ThermalSpec,
} from "../../lib/api/types";
import BuildingChart from "./BuildingChart";
import CarbonChart from "./CarbonChart";
import ExecutionPanel from "./ExecutionPanel";
import { readyByToDeadline } from "../../lib/jobs/normalize";
import Onboarding from "./Onboarding";

type Profile = { username?: string; occupation?: string; place?: string; rooms?: number | null; onboarded?: boolean };

type DashboardJob = JobInput & {
  tempMinC?: number;
  tempMaxC?: number;
  tempTargetC?: number;
};

/** What the progressive-disclosure form should show for a given class.
 *  Deliberately narrow: an ordinary user sees energy, duration, or comfort bands,
 *  never the raw decay coefficients or min-chunk setting. */
function fieldsFor(jobType: JobType | undefined, name = ""): {
  energy: boolean;
  duration: boolean;
  thermal: boolean;
  note: string;
} {
  const isThermal =
    jobType === "THERMAL" ||
    /heater|geyser|cool|ac\b|thermal|water heater|boiler/i.test(name);
  if (isThermal) {
    return {
      energy: false,
      duration: false,
      thermal: true,
      note: "Stores comfort as heat or cool — configured from its comfort band.",
    };
  }
  switch (jobType) {
    case "DEFERRABLE_INTERRUPTIBLE":
      return { energy: true, duration: false, thermal: false, note: "Pause and resume anywhere before the deadline." };
    case "DEFERRABLE_ATOMIC":
      return { energy: false, duration: true, thermal: false, note: "One continuous run once it starts." };
    case "FIXED":
      return { energy: false, duration: false, thermal: false, note: "Always-on: treated as background load, never shifted." };
    default:
      return { energy: false, duration: false, thermal: false, note: "" };
  }
}

function KindIcon({ kind }: { kind: string }) {
  const cls = "h-5 w-5";
  const k = kind.toLowerCase();
  let path: React.ReactNode;
  if (/ev|car|bike|charger|vehicle/.test(k)) {
    path = (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className={cls}>
        <path d="M4 16v-4.5L6.5 7h8L17 11.5H19a1.5 1.5 0 0 1 1.5 1.5v3H19" />
        <path d="M4 16h2.5M10.5 16h5" />
        <circle cx="8" cy="16.5" r="1.8" />
        <circle cx="16" cy="16.5" r="1.8" />
        <path d="M13 8.5 12 11h2l-1 2.5" />
      </svg>
    );
  } else if (/geyser|heater|water|boiler/.test(k)) {
    path = (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className={cls}>
        <rect x="7" y="2.5" width="10" height="16" rx="2.5" />
        <path d="M12 6.5c-1.6 2-2.6 3.2-2.6 4.6a2.6 2.6 0 0 0 5.2 0c0-1.4-1-2.6-2.6-4.6Z" />
        <path d="M10 21h4" />
      </svg>
    );
  } else if (/wash|laundry|dryer/.test(k)) {
    path = (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className={cls}>
        <rect x="4" y="2.5" width="16" height="19" rx="2.5" />
        <circle cx="12" cy="13" r="4.5" />
        <path d="M9.8 13a2.2 2.2 0 0 0 4.4 0" />
        <path d="M7.5 5.5h.01M10 5.5h4" />
      </svg>
    );
  } else if (/ac|cool|air|fan/.test(k)) {
    path = (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className={cls}>
        <rect x="3" y="4" width="18" height="6" rx="1.5" />
        <path d="M7 14c0 1.5 2 1.5 2 3s-2 1.5-2 3M12 14c0 1.5 2 1.5 2 3s-2 1.5-2 3M17 14c0 1.5 2 1.5 2 3s-2 1.5-2 3" />
      </svg>
    );
  } else {
    path = (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className={cls}>
        <path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12L13 2Z" />
      </svg>
    );
  }
  return (
    <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl border border-white/10 bg-black text-zinc-300">
      {path}
    </span>
  );
}

function BandChip({ band }: { band: RankedJob["band"] }) {
  const cls =
    band === "Critical"
      ? "bg-red-500/15 text-red-300"
      : band === "High"
        ? "bg-orange-400/15 text-orange-300"
        : band === "Normal"
          ? "bg-white/10 text-zinc-300"
          : "bg-white/5 text-zinc-500";
  return (
    <span className={`shrink-0 rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider ${cls}`}>{band}</span>
  );
}

const inputCls =
  "rounded-xl border border-white/10 bg-black px-4 py-2.5 text-sm text-white placeholder:text-zinc-700 focus:border-white/30 focus:outline-none";

/** localStorage key for the live schedule id, so a reload can rehydrate it. */
const ACTIVE_SCHEDULE_KEY = "heliotrope:active_schedule_id";

function getDefaultCoordinationData() {
  const slotMs = 15 * 60 * 1000;
  const now = new Date(Math.floor(Date.now() / slotMs) * slotMs);
  const dl = new Date(now.getTime() + 10 * 3600 * 1000);
  const participants = [
    { id: "unit-101", name: "Apt 101" },
    { id: "unit-102", name: "Apt 102" },
    { id: "unit-201", name: "Apt 201" },
  ];
  const coordJobs: LoadSpec[] = [
    {
      id: "bldg-ev-1",
      participant_id: "unit-101",
      user_input: "EV 1",
      normalized_name: "EV Charger 101",
      category: "EV charging",
      job_type: "DEFERRABLE_INTERRUPTIBLE",
      confidence: 1.0,
      ambiguous: false,
      power_kw: 7.2,
      max_power_kw: 7.2,
      energy_required_kwh: 14.4,
      min_chunk_minutes: 15,
      duration_minutes: null,
      release_at: now.toISOString(),
      deadline_at: dl.toISOString(),
      timezone: "UTC",
      thermal: null,
      explanation: "",
      assumptions: [],
      warnings: [],
      required_fields: [],
      alternatives: [],
    },
    {
      id: "bldg-wh-2",
      participant_id: "unit-102",
      user_input: "Geyser 102",
      normalized_name: "Geyser 102",
      category: "Water heating",
      job_type: "THERMAL",
      confidence: 1.0,
      ambiguous: false,
      power_kw: 2.0,
      max_power_kw: 2.0,
      energy_required_kwh: null,
      duration_minutes: null,
      min_chunk_minutes: null,
      release_at: now.toISOString(),
      deadline_at: dl.toISOString(),
      timezone: "UTC",
      thermal: {
        a: 0.9,
        b: 2.75,
        c: 2.0,
        max_power_kw: 2.0,
        resolution_minutes: 15,
        temperature_initial_c: 45.0,
        temperature_min_c: 40.0,
        temperature_max_c: 65.0,
        temperature_target_c: 55.0,
      },
      explanation: "",
      assumptions: [],
      warnings: [],
      required_fields: [],
      alternatives: [],
    },
    {
      id: "bldg-wash-3",
      participant_id: "unit-201",
      user_input: "Washer 201",
      normalized_name: "Laundry 201",
      category: "Laundry",
      job_type: "DEFERRABLE_ATOMIC",
      confidence: 1.0,
      ambiguous: false,
      power_kw: 2.5,
      max_power_kw: 2.5,
      duration_minutes: 60,
      energy_required_kwh: null,
      min_chunk_minutes: null,
      release_at: now.toISOString(),
      deadline_at: dl.toISOString(),
      timezone: "UTC",
      thermal: null,
      explanation: "",
      assumptions: [],
      warnings: [],
      required_fields: [],
      alternatives: [],
    },
  ];
  return { participants, coordJobs };
}

export default function Dashboard() {
  const [auth] = useState(() => getFirebaseAuth());
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(() => getFirebaseAuth() === null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileLoaded, setProfileLoaded] = useState(false);
  const [jobs, setJobs] = useState<DashboardJob[]>([]);
  const [ranked, setRanked] = useState<RankedJob[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [fName, setFName] = useState("");
  const [fPower, setFPower] = useState("");
  const [fReady, setFReady] = useState("06:00");
  const [fFlex, setFFlex] = useState(2);
  const [fEnergy, setFEnergy] = useState("");
  const [fDuration, setFDuration] = useState("");
  const [fTempMin, setFTempMin] = useState("");
  const [fTempMax, setFTempMax] = useState("");
  const [preview, setPreview] = useState<{ forName: string; jobType: JobType; category: string; confidence: number; ambiguous: boolean } | null>(null);
  // A preview is only usable for the exact name it was computed from, so a
  // stale one is ignored rather than cleared (clearing would mean a
  // synchronous setState inside the effect).
  const activePreview = preview && preview.forName === fName.trim() ? preview : null;
  const [signal, setSignal] = useState<CarbonSignalResponse | null>(null);
  const [signalError, setSignalError] = useState<string | null>(null);
  const [forecast, setForecast] = useState<CarbonForecastResponse | null>(null);
  const [forecastMode, setForecastMode] = useState<ForecastMode>("ACTUAL");
  const [forecastError, setForecastError] = useState<string | null>(null);
  const [riskWeight, setRiskWeight] = useState<number>(0.5);

  const [coordCapacity, setCoordCapacity] = useState("30");
  const [coordCapacityError, setCoordCapacityError] = useState<string | null>(null);
  const [coordIsDemo, setCoordIsDemo] = useState(false);
  const [coordResult, setCoordResult] = useState<CoordinationResult | null>(null);
  const [coordBusy, setCoordBusy] = useState(false);
  const [coordError, setCoordError] = useState<string | null>(null);

  const [liveId, setLiveId] = useState<string | null>(null);
  const [liveState, setLiveState] = useState<ExecutionState | null>(null);
  const [liveHistory, setLiveHistory] = useState<ScheduleHistory | null>(null);
  const [liveBusy, setLiveBusy] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveCapacity, setLiveCapacity] = useState("20");
  const [liveCapacityError, setLiveCapacityError] = useState<string | null>(null);
  const [addWarnings, setAddWarnings] = useState<string[]>([]);
  const [addErrors, setAddErrors] = useState<string[]>([]);
  const [fPowerError, setFPowerError] = useState<string | null>(null);
  const [plannedJobsSignature, setPlannedJobsSignature] = useState<string | null>(null);

  const loadAll = useCallback(async (u: User) => {
    const db = getDb();
    if (!db) {
      setProfileLoaded(true);
      return;
    }
    try {
      const snap = await getDoc(doc(db, "users", u.uid));
      setProfile(snap.exists() ? (snap.data() as Profile) : {});
    } catch {
      setProfile({});
    }
    try {
      const js = await getDocs(collection(db, "users", u.uid, "jobs"));
      setJobs(js.docs.map((d) => ({ id: d.id, ...(d.data() as Omit<DashboardJob, "id">) })));
    } catch {
      /* jobs unreadable (rules/offline) — onboarding still proceeds */
    }
    setProfileLoaded(true);
  }, []);

  useEffect(() => {
    if (!auth) return;
    return onAuthStateChanged(auth, (u) => {
      setUser(u);
      setReady(true);
      if (u) void loadAll(u);
    });
  }, [auth, loadAll]);

  const refreshLive = useCallback(async (id: string) => {
    try {
      const [s, h] = await Promise.all([getScheduleState(id), getScheduleHistory(id)]);
      setLiveState(s);
      setLiveHistory(h);
    } catch (e) {
      // Schedule is gone for good — don't rehydrate it on the next reload.
      if ((e as { status?: number }).status === 404) {
        localStorage.removeItem(ACTIVE_SCHEDULE_KEY);
      }
      throw e;
    }
  }, []);

  // Rehydrate a live session that survived a reload. A stale id (schedule
  // deleted, backend restarted, network down) is dropped quietly — the user
  // simply plans again.
  useEffect(() => {
    const savedId = localStorage.getItem(ACTIVE_SCHEDULE_KEY);
    if (!savedId) return;
    // Deferred into a microtask so the effect body itself stays free of state
    // updates; the effect only kicks off the rehydration.
    void Promise.resolve()
      .then(() => refreshLive(savedId))
      .then(() => setLiveId(savedId))
      .catch(() => {
        localStorage.removeItem(ACTIVE_SCHEDULE_KEY);
      });
  }, [refreshLive]);

  // Poll the live schedule forward once a minute. Each tick advances the
  // backend clock, then we re-read state — refresh only, no other side effects.
  useEffect(() => {
    if (!liveId) return;
    const t = setInterval(() => {
      void tickSchedule(liveId, new Date().toISOString())
        .then(() => refreshLive(liveId))
        .catch(() => {
          /* transient tick failure — next poll retries */
        });
    }, 60_000);
    return () => clearInterval(t);
  }, [liveId, refreshLive]);

  const loadsToSpecs = useCallback(() => {
    const slotMs = 15 * 60 * 1000;
    const now = new Date(Math.floor(Date.now() / slotMs) * slotMs);
    const specs: LoadSpec[] = [];
    const skipped: string[] = [];
    for (const j of jobs) {
      if (j.shiftable === false) {
        specs.push({
          id: j.id,
          normalized_name: j.name,
          category: j.kind || "Always-on",
          job_type: "FIXED",
          power_kw: j.powerKw,
          max_power_kw: j.powerKw,
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
          deadline_at: readyByToDeadline(j.readyBy, now).toISOString(),
          assumptions: [],
        });
        continue;
      }

      const isThermal =
        j.jobType === "THERMAL" ||
        /heater|geyser|cool|ac\b|thermal|water heater|boiler/i.test(`${j.name} ${j.kind || ""}`);

      if (isThermal) {
        const isAc = /cool|ac\b|air/i.test(`${j.name} ${j.kind || ""}`);
        const pKw = j.powerKw > 0 ? j.powerKw : (isAc ? 1.5 : 2.0);
        const minC = j.tempMinC ?? (isAc ? 22.0 : 40.0);
        const maxC = j.tempMaxC ?? (isAc ? 26.0 : 65.0);
        const tMin = Math.min(minC, maxC);
        const tMax = Math.max(minC, maxC);
        const tInit = isAc ? Math.max(tMax + 2, 30.0) : Math.min(tMin + 5, (tMin + tMax) / 2);
        const tTarget = (tMin + tMax) / 2;

        const thermalSpec: ThermalSpec = isAc
          ? {
              a: 0.85,
              b: -1.40,
              c: 5.10,
              max_power_kw: pKw,
              resolution_minutes: 15,
              temperature_initial_c: tInit,
              temperature_min_c: tMin,
              temperature_max_c: tMax,
              temperature_target_c: tTarget,
            }
          : {
              a: 0.90,
              b: 2.75,
              c: 2.0,
              max_power_kw: pKw,
              resolution_minutes: 15,
              temperature_initial_c: tInit,
              temperature_min_c: tMin,
              temperature_max_c: tMax,
              temperature_target_c: tTarget,
            };

        specs.push({
          id: j.id,
          normalized_name: j.name,
          category: j.kind || (isAc ? "Cooling" : "Water heating"),
          job_type: "THERMAL",
          power_kw: pKw,
          max_power_kw: pKw,
          duration_minutes: null,
          energy_required_kwh: null,
          min_chunk_minutes: null,
          confidence: 1.0,
          ambiguous: false,
          user_input: j.name,
          timezone: "UTC",
          thermal: thermalSpec,
          explanation: "",
          warnings: [],
          required_fields: [],
          alternatives: [],
          release_at: now.toISOString(),
          deadline_at: readyByToDeadline(j.readyBy, now).toISOString(),
          assumptions: [
            {
              field: "thermal",
              origin: j.tempMinC !== undefined ? "user-configured" : "synthetic default",
              detail: `comfort band ${tMin}°C–${tMax}°C`,
            },
          ],
        });
        continue;
      }

      const isInterruptible =
        j.jobType === "DEFERRABLE_INTERRUPTIBLE" ||
        (!j.jobType && /ev|charge|pump|laundry|wash/i.test(`${j.name} ${j.kind || ""}`));

      const hasUserEnergy =
        j.energyKwh !== undefined && !Number.isNaN(j.energyKwh) && j.energyKwh > 0;
      const hasUserDuration =
        j.durationMin !== undefined && !Number.isNaN(j.durationMin) && j.durationMin > 0;

      if (isInterruptible) {
        const energyKwh = hasUserEnergy ? j.energyKwh! : (j.powerKw > 0 ? j.powerKw * 2 : 2.0);
        specs.push({
          id: j.id,
          normalized_name: j.name,
          category: j.kind || "Flexible",
          job_type: "DEFERRABLE_INTERRUPTIBLE",
          power_kw: j.powerKw > 0 ? j.powerKw : 2.0,
          max_power_kw: j.powerKw > 0 ? j.powerKw : 2.0,
          energy_required_kwh: energyKwh,
          duration_minutes: hasUserDuration ? j.durationMin! : null,
          min_chunk_minutes: 15,
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
          deadline_at: readyByToDeadline(j.readyBy, now).toISOString(),
          assumptions: [
            {
              field: "energy_required_kwh",
              origin: hasUserEnergy ? "user-configured" : "synthetic default",
              detail: hasUserEnergy
                ? `user entered ${j.energyKwh} kWh`
                : "energy assumed = rating × 2h (frontend default — declare exact values later)",
            },
          ],
        });
      } else {
        const durationMin = hasUserDuration ? j.durationMin! : 60;
        specs.push({
          id: j.id,
          normalized_name: j.name,
          category: j.kind || "Flexible",
          job_type: "DEFERRABLE_ATOMIC",
          power_kw: j.powerKw > 0 ? j.powerKw : 1.5,
          max_power_kw: j.powerKw > 0 ? j.powerKw : 1.5,
          duration_minutes: durationMin,
          energy_required_kwh: hasUserEnergy ? j.energyKwh! : null,
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
          deadline_at: readyByToDeadline(j.readyBy, now).toISOString(),
          assumptions: [
            {
              field: "duration_minutes",
              origin: hasUserDuration ? "user-configured" : "synthetic default",
              detail: hasUserDuration
                ? `user entered ${j.durationMin} min`
                : "duration assumed 60 min (frontend default — declare exact values later)",
            },
          ],
        });
      }
    }
    return { specs, skipped };
  }, [jobs]);

  /** Empty means "use the default"; garbage must not silently fall back. */
  function parseCapacity(raw: string, fallback: number): { value?: number; error?: string } {
    const t = raw.trim();
    if (!t) return { value: fallback };
    const n = Number(t);
    if (!Number.isFinite(n) || n <= 0) return { error: "Enter a capacity greater than 0 kW." };
    if (n > 1000) return { error: "Enter a capacity up to 1000 kW." };
    return { value: n };
  }

  const jobsSignature = jobs
    .map((j) => `${j.id}:${j.powerKw}:${j.readyBy}:${j.flexHours}:${j.energyKwh ?? ""}:${j.durationMin ?? ""}:${j.tempMinC ?? ""}:${j.tempMaxC ?? ""}:${j.jobType ?? ""}:${j.shiftable ?? ""}`)
    .join("|");
  const loadsStale = liveId !== null && plannedJobsSignature !== null && jobsSignature !== plannedJobsSignature;

  async function planLive() {
    setLiveError(null);
    const cap = parseCapacity(liveCapacity, 20);
    if (cap.error || cap.value === undefined) {
      setLiveCapacityError(cap.error ?? "Enter a capacity greater than 0 kW.");
      return;
    }
    setLiveCapacityError(null);
    const { specs, skipped } = loadsToSpecs();
    if (!specs.length) {
      setLiveError(skipped.length ? "Only thermal loads present — they need a comfort band first." : "Add a load first.");
      return;
    }
    setLiveBusy(true);
    try {
      const carbonPayload =
        forecastMode === "ACTUAL"
          ? undefined
          : {
              mode: "FORECAST",
              forecast_model: "seasonal",
              forecast_mode: forecastMode,
              risk_weight: forecastMode === "ROBUST" ? riskWeight : 0.0,
            };

      const state = await planSchedule({
        jobs: specs,
        capacity_kw: cap.value,
        scheduler: "CPSAT",
        ...(carbonPayload ? { carbon: carbonPayload } : {}),
      });
      setLiveId(state.schedule_id);
      localStorage.setItem(ACTIVE_SCHEDULE_KEY, state.schedule_id);
      setPlannedJobsSignature(jobsSignature);
      await refreshLive(state.schedule_id);
      if (skipped.length) setLiveError(`Thermal skipped for now: ${skipped.join(", ")} — comfort band unknown.`);
    } catch (e) {
      setLiveError(e instanceof Error ? e.message : "Planning failed.");
    } finally {
      setLiveBusy(false);
    }
  }

  async function liveAdvance() {
    if (!liveId) return;
    setLiveBusy(true);
    try {
      await advanceSimulation(liveId, { to_time: new Date(Date.now() + 15 * 60 * 1000).toISOString(), script: [] });
      await refreshLive(liveId);
    } catch (e) {
      setLiveError(e instanceof Error ? e.message : "Advance failed.");
    } finally {
      setLiveBusy(false);
    }
  }

  async function liveReplan() {
    if (!liveId) return;
    setLiveBusy(true);
    try {
      await replanSchedule(liveId, { reason: "MANUAL" });
      await refreshLive(liveId);
    } catch (e) {
      setLiveError(e instanceof Error ? e.message : "Replan failed.");
    } finally {
      setLiveBusy(false);
    }
  }

  async function liveEvent(jobId: string, type: string) {
    if (!liveId) return;
    setLiveBusy(true);
    try {
      await postScheduleEvent(liveId, { event_type: type, job_id: jobId });
      await refreshLive(liveId);
    } catch (e) {
      setLiveError(e instanceof Error ? e.message : "Event failed.");
    } finally {
      setLiveBusy(false);
    }
  }

  const runCoordination = useCallback(
    async (capKw?: number) => {
      setCoordBusy(true);
      setCoordError(null);
      try {
        const parsed =
          capKw !== undefined ? { value: capKw } : parseCapacity(coordCapacity, 30);
        if (parsed.error || parsed.value === undefined || !(parsed.value > 0)) {
          setCoordCapacityError(parsed.error ?? "Enter a capacity greater than 0 kW.");
          return;
        }
        setCoordCapacityError(null);
        const cap = parsed.value;
        const { specs } = loadsToSpecs();

        let coordJobs: LoadSpec[] = [];
        let participants: Array<{ id: string; name: string }> = [];

        if (specs.length > 0) {
          participants = specs.map((s, idx) => ({
            id: `unit-${idx + 1}`,
            name: `Unit ${idx + 1}`,
          }));
          coordJobs = specs.map((s, idx) => ({
            ...s,
            participant_id: `unit-${idx + 1}`,
          }));
        } else {
          const defaults = getDefaultCoordinationData();
          participants = defaults.participants;
          coordJobs = defaults.coordJobs;
        }

        const res = await coordinateBuilding({
          participants,
          shared_resource: { capacity_kw: cap },
          jobs: coordJobs,
        });
        setCoordResult(res);
        setCoordIsDemo(specs.length === 0);
      } catch (err) {
        setCoordError(err instanceof Error ? err.message : "Coordination failed.");
      } finally {
        setCoordBusy(false);
      }
    },
    [coordCapacity, loadsToSpecs]
  );

  useEffect(() => {
    let cancelled = false;
    const end = new Date();
    const start = new Date(end.getTime() - 24 * 3600 * 1000);
    getCarbonSignal({ start: start.toISOString(), end: end.toISOString() })
      .then((s) => {
        if (!cancelled) setSignal(s);
      })
      .catch(() => {
        if (!cancelled) setSignalError("Backend signal unreachable — is it running?");
      });

    const fStart = new Date();
    const fEnd = new Date(fStart.getTime() + 24 * 3600 * 1000);
    getCarbonForecast({ start: fStart.toISOString(), end: fEnd.toISOString() })
      .then((f) => {
        if (cancelled) return;
        setForecast(f);
        setForecastError(null);
      })
      .catch(() => {
        if (cancelled) return;
        setForecast(null);
        setForecastError("Forecast unavailable — showing actual signal only.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Demo coordination runs only when the user has no loads: the empty state
  // below explains the demo, and a user with jobs must press "Coordinate
  // building" themselves — never a silent demo-first-paint over real data.
  useEffect(() => {
    if (!profileLoaded) return;
    if (jobs.length > 0) return;
    if (coordResult || coordBusy) return;
    // Deferred so the effect body itself stays free of synchronous state
    // updates; the demo fetch resolves in a callback.
    void Promise.resolve().then(() => runCoordination(30));
  }, [profileLoaded, jobs.length, coordResult, coordBusy, runCoordination]);

  // Progressive disclosure: ask the backend what this name is, debounced, and
  // reveal only the fields that class actually needs. Local `classifyJob`
  // still renders instantly so the form stays usable when the backend is down.
  useEffect(() => {
    const name = fName.trim();
    if (name.length < 2) return;
    let cancelled = false;
    const t = setTimeout(() => {
      classifyLoad({ name, deadline_wall: fReady })
        .then((r) => {
          if (cancelled) return;
          setPreview({
            forName: name,
            jobType: r.classification.job_type,
            category: r.classification.category,
            confidence: r.confidence,
            ambiguous: r.ambiguous,
          });
        })
        .catch(() => {
          // Backend classification is an enhancement, not a requirement: the
          // local heuristic already renders and the form still works.
          if (!cancelled) setPreview(null);
        });
    }, 350);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [fName, fReady]);

  // Validate the in-progress load against the backend before adding, so
  // feasibility errors/warnings show inline in the form (never silent).
  useEffect(() => {
    const name = fName.trim();
    if (name.length < 2 || !fReady) {
      // Deferred so the effect body itself stays free of synchronous state
      // updates; clearing happens in a microtask instead.
      void Promise.resolve().then(() => {
        setAddErrors([]);
        setAddWarnings([]);
      });
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      const c = classifyJob(name);
      const thermalGuess = /heater|geyser|cool|ac\b|thermal|water heater|boiler/i.test(
        `${name} ${activePreview?.category ?? c.category}`
      );
      const jobType = activePreview?.jobType
        ?? (thermalGuess ? "THERMAL" : c.shiftable ? "DEFERRABLE_INTERRUPTIBLE" : "FIXED");
      const pKw = Number(fPower);
      const eKwh = fEnergy ? Number(fEnergy) : null;
      const dMin = fDuration ? Math.round(Number(fDuration)) : null;
      const slotMs = 15 * 60 * 1000;
      const now = new Date(Math.floor(Date.now() / slotMs) * slotMs);
      const isThermalForm = jobType === "THERMAL";
      const isAcForm = /cool|ac\b|air/i.test(`${name} ${activePreview?.category ?? c.category}`);
      const tMin = fTempMin ? Number(fTempMin) : isThermalForm ? (isAcForm ? 22 : 40) : NaN;
      const tMax = fTempMax ? Number(fTempMax) : isThermalForm ? (isAcForm ? 26 : 65) : NaN;
      const spec: LoadSpec = {
        id: "form-preview",
        normalized_name: name,
        category: activePreview?.category ?? c.category,
        job_type: jobType,
        power_kw: Number.isFinite(pKw) && pKw > 0 ? pKw : null,
        max_power_kw: Number.isFinite(pKw) && pKw > 0 ? pKw : null,
        duration_minutes: dMin,
        energy_required_kwh: eKwh,
        min_chunk_minutes: jobType === "DEFERRABLE_INTERRUPTIBLE" ? 15 : null,
        confidence: activePreview?.confidence ?? 1.0,
        ambiguous: activePreview?.ambiguous ?? false,
        user_input: name,
        timezone: "UTC",
        thermal: isThermalForm
          ? isAcForm
            ? {
                a: 0.85,
                b: -1.40,
                c: 5.10,
                max_power_kw: Number.isFinite(pKw) && pKw > 0 ? pKw : 2.0,
                resolution_minutes: 15,
                temperature_initial_c: null,
                temperature_min_c: Number.isFinite(tMin) ? tMin : 22,
                temperature_max_c: Number.isFinite(tMax) ? tMax : 26,
                temperature_target_c: null,
              }
            : {
                a: 0.9,
                b: 2.75,
                c: 2.0,
                max_power_kw: Number.isFinite(pKw) && pKw > 0 ? pKw : 2.0,
                resolution_minutes: 15,
                temperature_initial_c: null,
                temperature_min_c: Number.isFinite(tMin) ? tMin : 40,
                temperature_max_c: Number.isFinite(tMax) ? tMax : 65,
                temperature_target_c: null,
              }
          : null,
        explanation: "",
        warnings: [],
        required_fields: [],
        alternatives: [],
        release_at: now.toISOString(),
        deadline_at: readyByToDeadline(fReady, now).toISOString(),
        assumptions: [],
      };
      validateLoad(spec)
        .then((r) => {
          if (cancelled) return;
          setAddErrors(r.errors.map((e) => e.message));
          setAddWarnings(r.warnings.map((w) => w.message));
        })
        .catch(() => {
          // Backend validation is advisory; the form still works offline.
          if (!cancelled) {
            setAddErrors([]);
            setAddWarnings([]);
          }
        });
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [fName, fReady, fPower, fEnergy, fDuration, fTempMin, fTempMax, activePreview]);

  async function addJob() {
    if (!user || !fName.trim() || !fReady) return;
    // Backend feasibility errors block the add; warnings stay advisory.
    // (Offline the validator stays silent, so addErrors is empty and the add proceeds.)
    if (addErrors.length > 0) return;
    const powerKw = Number(fPower);
    if (!fPower.trim() || !Number.isFinite(powerKw) || powerKw <= 0) {
      setFPowerError("Enter a power rating greater than 0 kW.");
      return;
    }
    setFPowerError(null);
    const db = getDb();
    if (!db) return;
    setAdding(true);
    const c = classifyJob(fName);
    // Backend classification wins when it is available; the local heuristic is
    // the offline fallback. The stored `kind`/`shiftable` stay for every
    // document written before Phase 3.
    const jobType = activePreview?.jobType;
    const shiftable = jobType ? jobType !== "FIXED" : c.shiftable;
    const energyKwh = fEnergy ? Number(fEnergy) : undefined;
    const durationMin = fDuration ? Math.round(Number(fDuration)) : undefined;

    const isThermal =
      jobType === "THERMAL" ||
      /heater|geyser|cool|ac\b|thermal|water heater|boiler/i.test(
        `${fName} ${activePreview?.category ?? c.category}`
      );
    const isAc = /cool|ac\b|air/i.test(`${fName} ${activePreview?.category ?? c.category}`);
    const tempMinC = fTempMin ? Number(fTempMin) : (isThermal ? (isAc ? 22 : 40) : undefined);
    const tempMaxC = fTempMax ? Number(fTempMax) : (isThermal ? (isAc ? 26 : 65) : undefined);

    try {
      const ref = await addDoc(collection(db, "users", user.uid, "jobs"), {
        name: fName.trim(),
        kind: activePreview?.category ?? c.category,
        shiftable,
        powerKw,
        readyBy: fReady,
        flexHours: fFlex,
        // Phase 3 fields: written only when known, so a missing value stays
        // missing instead of becoming a misleading zero.
        ...(jobType ? { jobType } : {}),
        ...(energyKwh && !Number.isNaN(energyKwh) ? { energyKwh } : {}),
        ...(durationMin && !Number.isNaN(durationMin) ? { durationMin } : {}),
        ...(tempMinC !== undefined && !Number.isNaN(tempMinC) ? { tempMinC } : {}),
        ...(tempMaxC !== undefined && !Number.isNaN(tempMaxC) ? { tempMaxC } : {}),
        ...(activePreview ? { confidence: activePreview.confidence } : {}),
        createdAt: new Date().toISOString(),
      });
      setJobs((js) => [
        ...js,
        {
          id: ref.id,
          name: fName.trim(),
          kind: activePreview?.category ?? c.category,
          shiftable,
          powerKw,
          readyBy: fReady,
          flexHours: fFlex,
          jobType,
          energyKwh,
          durationMin,
          tempMinC,
          tempMaxC,
        },
      ]);
      setRanked(null);
      setFName("");
      setFEnergy("");
      setFDuration("");
      setFTempMin("");
      setFTempMax("");
      setPreview(null);
      setAddErrors([]);
      setAddWarnings([]);
      setFPowerError(null);
    } finally {
      setAdding(false);
    }
  }

  async function removeJob(id: string) {
    if (!user) return;
    const db = getDb();
    if (!db) return;
    const prev = jobs;
    setJobs((js) => js.filter((j) => j.id !== id));
    setRanked(null);
    try {
      await deleteDoc(doc(db, "users", user.uid, "jobs", id));
    } catch {
      // Restore the row: the delete never landed (rules/offline), so the
      // list must still show it.
      setJobs(prev);
    }
  }

  if (!ready) {
    return (
      <main className="grid min-h-screen place-items-center bg-black text-zinc-500">
        <p className="font-mono text-[12px]">loading…</p>
      </main>
    );
  }

  if (!user) {
    return (
      <main className="grid min-h-screen place-items-center bg-black px-6 text-center text-zinc-100">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-zinc-600">Heliotrope · dashboard</p>
          <h1 className="mt-4 text-3xl font-semibold tracking-tight">Sign in to continue</h1>
          <button
            onClick={async () => {
              setBusy(true);
              try {
                const auth = getFirebaseAuth();
                if (auth) await signInWithPopup(auth, getGoogleProvider());
              } finally {
                setBusy(false);
              }
            }}
            disabled={busy}
            className="mt-7 flex cursor-pointer items-center justify-center gap-2 rounded-full bg-white px-7 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-70"
          >
            {busy && <span className="spinner" />}
            {busy ? "Signing in…" : "Sign in with Google"}
          </button>
          <p className="mt-6 text-[13px]">
            <Link href="/" className="text-zinc-500 transition hover:text-white">← back home</Link>
          </p>
        </div>
      </main>
    );
  }

  const needsOnboarding = profileLoaded && (!profile || !profile.onboarded);

  return (
    <main className="min-h-screen bg-black text-zinc-100">
      {needsOnboarding && <Onboarding user={user} onDone={() => void loadAll(user)} />}
      <div className="mx-auto max-w-7xl px-6 py-6 sm:px-10 lg:px-16">
        <header className="flex items-center justify-between">
          <Link href="/" className="text-[13px] font-semibold uppercase tracking-[0.28em]">Heliotrope</Link>
          <div className="relative flex items-center gap-3">
            {user.photoURL ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={user.photoURL} alt="" referrerPolicy="no-referrer" className="h-8 w-8 rounded-full bg-white/10 object-cover" />
            ) : null}
            <span className="max-w-40 truncate text-[13px] text-zinc-300">{user.displayName ?? user.email}</span>
            <button
              onClick={() => setMenuOpen((v) => !v)}
              aria-label="Account menu"
              className="grid h-8 w-8 cursor-pointer place-items-center text-lg leading-none text-zinc-300 transition hover:text-white active:scale-95"
            >
              ⋮
            </button>
            {menuOpen && (
              <>
                <button aria-label="Close menu" onClick={() => setMenuOpen(false)} className="fixed inset-0 z-10 cursor-default" />
                <div className="absolute right-0 top-10 z-20 w-44 overflow-hidden rounded-xl border border-white/10 bg-[#111] shadow-xl shadow-black/50">
                  <Link
                    href="/account"
                    className="block px-4 py-2.5 text-[13px] text-zinc-300 transition hover:bg-white/5 hover:text-white"
                  >
                    Account
                  </Link>
                  <button
                    onClick={async () => {
                      if (!auth) return;
                      setBusy(true);
                      try {
                        await signOut(auth);
                      } finally {
                        setBusy(false);
                      }
                    }}
                    disabled={busy}
                    className="block w-full cursor-pointer px-4 py-2.5 text-left text-[13px] text-zinc-300 transition hover:bg-white/5 hover:text-white active:bg-white/10 disabled:cursor-wait disabled:opacity-60"
                  >
                    {busy ? "Signing out…" : "Sign out"}
                  </button>
                </div>
              </>
            )}
          </div>
        </header>

        <h1 className="mt-12 text-3xl font-semibold tracking-tight sm:text-4xl">
          Evening, {(user.displayName ?? "there").split(" ")[0]}.
        </h1>
        <p className="mt-3 max-w-lg text-[15px] leading-7 text-zinc-500">
          {profile?.place ? `${profile.place}${profile.rooms ? ` · ${profile.rooms} rooms` : ""} — ` : ""}
          your loads, ranked by priority. Timed schedules come from the live planner below.
        </p>

        {/* loads */}
        <section className="mt-10 overflow-hidden rounded-2xl border border-white/10 bg-[#0a0a0a]">
          <div className="flex flex-wrap items-center justify-between gap-3 px-6 py-5 sm:px-8">
            <div>
              <h2 className="text-[15px] font-medium">Your loads</h2>
              <p className="mt-0.5 font-mono text-[11px] text-zinc-600">
                {jobs.length === 0 ? "nothing added yet" : `${jobs.length} load${jobs.length > 1 ? "s" : ""}${ranked ? " · ranked" : ""}`}
              </p>
            </div>
            <button
              onClick={() => setRanked(jevRank(jobs))}
              disabled={jobs.length === 0}
              className="cursor-pointer rounded-full bg-lime-300 px-6 py-2 text-[13px] font-medium text-black transition hover:bg-lime-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
            >
              Rank
            </button>
          </div>

          {(ranked ? [...ranked, ...jobs.filter((j) => j.shiftable === false)] : jobs).map((item, i) => {
            const j = item as DashboardJob & Partial<RankedJob>;
            return (
              <div key={j.id} className="group flex items-center gap-5 border-t border-white/5 px-6 py-5 transition hover:bg-white/[0.02] sm:px-8">
                {ranked && j.shiftable !== false && <span className="w-6 shrink-0 font-mono text-[13px] text-zinc-600">{String(i + 1).padStart(2, "0")}</span>}
                <KindIcon kind={j.kind} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <p className="text-[15px] font-medium">{j.name}</p>
                    {j.shiftable === false ? (
                      <span className="shrink-0 rounded-full bg-white/5 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">always-on · filtered</span>
                    ) : (
                      ranked && j.band && <BandChip band={j.band} />
                    )}
                  </div>
                  <p className="mt-1.5 text-[13px] text-zinc-500">
                    {j.kind || "Load"} · {j.powerKw} kW · ready by {j.readyBy} · +{j.flexHours}h flexible
                    {j.energyKwh ? ` · ${j.energyKwh} kWh` : ""}
                    {j.durationMin ? ` · ${j.durationMin} min` : ""}
                    {j.tempMinC !== undefined && j.tempMaxC !== undefined ? ` · comfort ${j.tempMinC}°C–${j.tempMaxC}°C` : ""}
                  </p>
                  {ranked && j.shiftable !== false && (
                    <>
                      <div className="mt-2.5 h-1 max-w-md overflow-hidden rounded-full bg-white/10">
                        <div className="h-full rounded-full bg-lime-300 transition-[width] duration-700" style={{ width: `${j.score}%` }} />
                      </div>
                      <p className="mt-1.5 text-[13px] text-zinc-500">{j.reason}</p>
                    </>
                  )}
                </div>
                <button onClick={() => void removeJob(j.id)} className="shrink-0 cursor-pointer font-mono text-[11px] text-zinc-700 transition hover:text-red-300 focus-visible:opacity-100 focus-visible:text-red-300 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100">
                  remove
                </button>
              </div>
            );
          })}

          {jobs.length === 0 && (
            <p className="border-t border-white/5 px-6 py-6 text-sm leading-6 text-zinc-500 sm:px-8">
              No loads yet. Name your first one below — it takes ten seconds.
            </p>
          )}

          {/* add */}
          <div className="border-t border-white/10 bg-white/[0.015] px-6 py-6 sm:px-8">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Add a load — name anything</p>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              <input value={fName} onChange={(e) => setFName(e.target.value)} maxLength={40} placeholder="Name — e.g. hostel borewell pump" className={inputCls} />
              <div className={`flex items-center px-4 ${inputCls} ${fName.trim() ? "" : "opacity-40"}`}>
                {(() => {
                  const c = classifyJob(fName.trim() || "…");
                  return (
                    <>
                      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${c.shiftable ? "bg-lime-300" : "bg-zinc-600"}`} />
                      <span className="ml-2 truncate text-sm text-zinc-200">
                        {fName.trim() ? `${c.category} · ${c.why}` : "type a name — local rules file it"}
                      </span>
                    </>
                  );
                })()}
              </div>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-3">
              <label className="flex flex-col gap-1 text-[13px] text-zinc-400">
                <span className="flex items-center gap-2">
                  <input value={fPower} onChange={(e) => { setFPower(e.target.value.replace(/[^0-9.]/g, "")); if (fPowerError) setFPowerError(null); }} inputMode="decimal" placeholder="kW" className={`w-20 ${inputCls}`} />
                  <span className="font-mono text-[11px] text-zinc-600">kW rating</span>
                </span>
                {fPowerError && (
                  <span className="font-mono text-[11px] text-orange-300">{fPowerError}</span>
                )}
              </label>
              <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                <span className="font-mono text-[11px] text-zinc-600">ready by</span>
                <input type="time" value={fReady} onChange={(e) => setFReady(e.target.value)} className={`cursor-pointer [color-scheme:dark] ${inputCls}`} />
              </label>
              <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                <span className="font-mono text-[11px] text-zinc-600">+{fFlex}h flex</span>
                <input type="range" min={0} max={6} value={fFlex} aria-label="Flexibility in hours" onChange={(e) => setFFlex(Number(e.target.value))} className="w-28 cursor-pointer accent-lime-300" />
              </label>
              <button
                onClick={() => void addJob()}
                disabled={adding || !fName.trim() || !fReady || addErrors.length > 0}
                title={addErrors.length > 0 ? "Fix the errors above before adding." : undefined}
                className="flex cursor-pointer items-center gap-2 rounded-full bg-white px-6 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
              >
                {adding && <span className="spinner" />}
                {adding ? "Adding…" : "Add load"}
              </button>
            </div>

            {/* Progressive disclosure: only the fields this class actually needs. */}
            {(() => {
              const f = fieldsFor(activePreview?.jobType, fName);
              if (!f.energy && !f.duration && !f.thermal && !f.note) return null;
              return (
                <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-3 border-t border-white/5 pt-4">
                  {f.energy && (
                    <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                      <input
                        value={fEnergy}
                        onChange={(e) => setFEnergy(e.target.value.replace(/[^0-9.]/g, ""))}
                        inputMode="decimal"
                        placeholder="kWh"
                        className={`w-20 ${inputCls}`}
                      />
                      <span className="font-mono text-[11px] text-zinc-600">energy needed</span>
                    </label>
                  )}
                  {f.duration && (
                    <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                      <input
                        value={fDuration}
                        onChange={(e) => setFDuration(e.target.value.replace(/[^0-9]/g, ""))}
                        inputMode="numeric"
                        placeholder="min"
                        className={`w-20 ${inputCls}`}
                      />
                      <span className="font-mono text-[11px] text-zinc-600">run length</span>
                    </label>
                  )}
                  {f.thermal && (
                    <>
                      <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                        <input
                          value={fTempMin}
                          onChange={(e) => setFTempMin(e.target.value.replace(/[^0-9.]/g, ""))}
                          inputMode="decimal"
                          placeholder={/cool|ac\b|air/i.test(fName) ? "22" : "40"}
                          className={`w-20 ${inputCls}`}
                        />
                        <span className="font-mono text-[11px] text-zinc-600">min temp °C</span>
                      </label>
                      <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                        <input
                          value={fTempMax}
                          onChange={(e) => setFTempMax(e.target.value.replace(/[^0-9.]/g, ""))}
                          inputMode="decimal"
                          placeholder={/cool|ac\b|air/i.test(fName) ? "26" : "65"}
                          className={`w-20 ${inputCls}`}
                        />
                        <span className="font-mono text-[11px] text-zinc-600">max temp °C</span>
                      </label>
                    </>
                  )}
                  {f.note && <span className="font-mono text-[11px] text-zinc-600">{f.note}</span>}
                  {activePreview?.ambiguous && (
                    <span className="font-mono text-[11px] text-amber-400/80">
                      could mean something else — {Math.round(activePreview.confidence * 100)}% sure
                    </span>
                  )}
                </div>
              );
            })()}
            {(addErrors.length > 0 || addWarnings.length > 0) && (
              <div className="mt-3 space-y-1 border-t border-white/5 pt-3">
                {addErrors.map((m, i) => (
                  <p key={`e-${i}`} className="font-mono text-[11px] text-red-300">{m}</p>
                ))}
                {addWarnings.map((m, i) => (
                  <p key={`w-${i}`} className="font-mono text-[11px] text-amber-400/80">{m}</p>
                ))}
              </div>
            )}
          </div>
        </section>

        <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-6 sm:p-8">
          {signal ? (
            <>
              <CarbonChart
                signal={signal}
                forecast={forecast}
                mode={forecastMode}
                onModeChange={setForecastMode}
                riskWeight={riskWeight}
                onRiskWeightChange={setRiskWeight}
              />
              {forecastError && (
                <p className="mt-2 font-mono text-[11px] text-amber-400/80">{forecastError}</p>
              )}
            </>
          ) : signalError ? (
            <p className="font-mono text-[12px] text-zinc-600">{signalError}</p>
          ) : (
            <p className="font-mono text-[12px] text-zinc-600">reading grid signal…</p>
          )}
        </div>

        <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-6 sm:p-8">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-medium">Live schedule</h2>
              <p className="mt-0.5 font-mono text-[11px] text-zinc-600">
                {liveState ? `${liveState.lifecycle} · v${liveState.version}` : "plans your loads on the backend, then tracks execution"}
              </p>
            </div>
            {!liveId && (
              <div className="flex items-start gap-2">
                <label className="flex flex-col gap-1 text-[13px] text-zinc-400">
                  <span className="flex items-center gap-2">
                    <input value={liveCapacity} aria-label="Live schedule capacity in kilowatts" onChange={(e) => setLiveCapacity(e.target.value.replace(/[^0-9.]/g, ""))} inputMode="decimal" className={`w-20 ${inputCls}`} />
                    <span className="font-mono text-[11px] text-zinc-600">kW</span>
                  </span>
                  {liveCapacityError && (
                    <span className="font-mono text-[11px] text-orange-300">{liveCapacityError}</span>
                  )}
                </label>
                <button
                  onClick={() => void planLive()}
                  disabled={liveBusy || jobs.length === 0}
                  className="cursor-pointer rounded-full bg-white px-5 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
                >
                  {liveBusy ? "Planning…" : "Plan live"}
                </button>
              </div>
            )}
          </div>
          {liveError && <p aria-live="polite" className="mt-3 font-mono text-[12px] text-orange-300">{liveError}</p>}
          {loadsStale && liveState && (
            <p aria-live="polite" className="mt-3 font-mono text-[12px] text-amber-400/90">
              Loads changed since v{liveState.version} — re-plan to refresh the schedule.
            </p>
          )}
          {liveState && (
            <div className="mt-4 border-t border-white/10 pt-4">
              <ExecutionPanel
                state={liveState}
                history={liveHistory}
                busy={liveBusy}
                onAdvance={() => void liveAdvance()}
                onReplan={() => void liveReplan()}
                onEvent={(jobId, type) => void liveEvent(jobId, type)}
              />
            </div>
          )}
        </div>

        {/* Multi-user building coordination */}
        <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-6 sm:p-8">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-[15px] font-medium">Building coordination</h2>
                <span className="rounded-full bg-lime-300/15 px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-lime-300">
                  {coordResult?.status ?? "Ready"}
                </span>
              </div>
              <p className="mt-0.5 font-mono text-[11px] text-zinc-600">
                Multi-user scheduling under shared feeder capacity constraint
              </p>
            </div>
            <div className="flex items-start gap-2">
              <label className="flex flex-col gap-1 text-[13px] text-zinc-400">
                <span className="flex items-center gap-2">
                  <input
                    value={coordCapacity}
                    aria-label="Building capacity limit in kilowatts"
                    onChange={(e) => setCoordCapacity(e.target.value.replace(/[^0-9.]/g, ""))}
                    inputMode="decimal"
                    placeholder="30"
                    className={`w-20 ${inputCls}`}
                  />
                  <span className="font-mono text-[11px] text-zinc-600">kW limit</span>
                </span>
                {coordCapacityError && (
                  <span className="font-mono text-[11px] text-orange-300">{coordCapacityError}</span>
                )}
              </label>
              <button
                onClick={() => void runCoordination()}
                disabled={coordBusy}
                className="cursor-pointer rounded-full bg-white px-5 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
              >
                {coordBusy ? "Coordinating…" : "Coordinate building"}
              </button>
            </div>
          </div>

          {coordError && <p aria-live="polite" className="mt-3 font-mono text-[12px] text-orange-300">{coordError}</p>}

          {coordResult ? (
            <div className="mt-4 border-t border-white/5 pt-4">
              <div className="mb-4 flex flex-wrap items-center gap-4 font-mono text-[11px] text-zinc-400">
                <span>
                  Mode: <strong className="text-zinc-200">{coordResult.coordination_mode}</strong>
                </span>
                <span
                  className={`rounded-full px-2.5 py-0.5 uppercase tracking-wider ${
                    coordIsDemo ? "bg-white/10 text-zinc-300" : "bg-lime-300/15 text-lime-300"
                  }`}
                >
                  {coordIsDemo ? "demo data" : "your loads"}
                </span>
                <span>
                  Tenants: <strong className="text-zinc-200">{coordResult.participants.length}</strong>
                </span>
                <span>
                  Loads: <strong className="text-zinc-200">{coordResult.jobs.length}</strong>
                </span>
                <span>
                  Peak: <strong className="text-zinc-200">{coordResult.metrics.peak_kw?.toFixed(1) ?? "—"} kW</strong>
                </span>
                <span>
                  Capacity violations:{" "}
                  <strong className={coordResult.metrics.capacity_violations === 0 ? "text-lime-300" : "text-red-400"}>
                    {coordResult.metrics.capacity_violations}
                  </strong>
                </span>
              </div>
              <BuildingChart points={coordResult.aggregate_profile} />
              {coordIsDemo && (
                <p className="mt-2 font-mono text-[11px] text-zinc-600">
                  Demo preview — add your own loads and press “Coordinate building” to replace it.
                </p>
              )}
            </div>
          ) : (
            <div className="mt-4 border-t border-white/5 pt-4">
              <p className="mb-3 font-mono text-[11px] text-zinc-600">
                {jobs.length === 0
                  ? "No loads yet — the demo preview loads here, or add a load above."
                  : "No coordination yet — press “Coordinate building” to schedule your loads."}
              </p>
              <BuildingChart points={[]} />
            </div>
          )}
        </div>

        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <div className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-lime-300/80">Connected</p>
            <h2 className="mt-2 text-[15px] font-medium">Account</h2>
            <p className="mt-2 break-all font-mono text-[12px] text-zinc-500">{user.email}</p>
            <p className="mt-1 font-mono text-[11px] text-zinc-700">{process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID}</p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Not connected</p>
            <h2 className="mt-2 text-[15px] font-medium">Meters</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">No meter stream yet. First plug meter that reports in appears here.</p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-lime-300/80">Active</p>
            <h2 className="mt-2 text-[15px] font-medium">Coordination</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">Multi-user feeder optimization active above. Feeder ceiling enforced jointly.</p>
          </div>
        </div>
      </div>
    </main>
  );
}
