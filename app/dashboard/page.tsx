"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { onAuthStateChanged, signInWithPopup, signOut, type User } from "firebase/auth";
import { addDoc, collection, deleteDoc, doc, getDoc, getDocs, updateDoc } from "firebase/firestore";
import { getDb, getFirebaseAuth, getGoogleProvider } from "../../lib/firebase";
import { jevRank, classifyJob, hoursUntilReady, type JobInput, type RankedJob } from "../../lib/prioritize";
import {
  compareSchedulers,
  coordinateBuilding,
  getCarbonForecast,
  getCarbonSignal,
  getScheduleHistory,
  getScheduleState,
  prioritizeLoads,
  planSchedule,
  postScheduleEvent,
  overrideSchedule,
  replanSchedule,
  tickSchedule,
  validateLoad,
  waitForBackend,
  type CarbonForecastResponse,
  type CompareSchedulerResult,
  type ForecastMode,
} from "../../lib/api/client";
import type {
  CarbonSignalResponse,
  CoordinationResult,
  ExecutionState,
  PoolSummary,
  JobType,
  LoadSpec,
  OverrideCommand,
  ScheduleHistory,
} from "../../lib/api/types";
import BuildingChart from "./BuildingChart";
import CarbonChart from "./CarbonChart";
import ExecutionPanel from "./ExecutionPanel";
import { AC_RE, buildSpecs, detailNeeded, kindOf, latestFinish, nextSlot, type Detail, type StoredJob } from "../../lib/loads/specs";
import { useLoadClassification } from "./useLoadClassification";
import { loadTypeLabel } from "./labels";
import Onboarding from "./Onboarding";
import { scrubLegacyEmail } from "../../lib/profile";

type Profile = { username?: string; occupation?: string; place?: string; rooms?: number | null; onboarded?: boolean };

type DashboardJob = JobInput & {
  tempMinC?: number;
  tempMaxC?: number;
  tempTargetC?: number;
};

/** What the progressive-disclosure form should show for a given class.
 *  Deliberately narrow: an ordinary user sees energy, run length, or comfort
 *  temperatures, never the raw decay coefficients or min-chunk setting. */
function fieldsFor(jobType: JobType | undefined): {
  energy: boolean;
  duration: boolean;
  thermal: boolean;
  note: string;
} {
  switch (jobType) {
    case "THERMAL":
      return {
        energy: false,
        duration: false,
        thermal: true,
        note: "Keeps a temperature: tell us the range that is comfortable and we heat or cool inside it.",
      };
    case "DEFERRABLE_INTERRUPTIBLE":
      return { energy: true, duration: false, thermal: false, note: "Can be paused and resumed any time before it is needed." };
    case "DEFERRABLE_ATOMIC":
      return { energy: false, duration: true, thermal: false, note: "Once it starts, it runs straight through without stopping." };
    case "FIXED":
      return { energy: false, duration: false, thermal: false, note: "Always on: it is counted as background use and never moved." };
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
    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl border border-white/10 bg-black text-zinc-300 sm:h-12 sm:w-12">
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
  "min-w-0 max-w-full rounded-xl border border-white/10 bg-black px-4 py-2.5 text-sm text-white placeholder:text-zinc-700 focus:border-white/30 focus:outline-none";

/** localStorage key for the live schedule id, so a reload can rehydrate it. */
const ACTIVE_SCHEDULE_KEY = "heliotrope:active_schedule_id";
/** The impact comparison for that schedule, so a reload doesn't drop it. */
const ACTIVE_IMPACT_KEY = "heliotrope:active_impact";
/** Fingerprint of the loads the active plan was built from, to flag a stale plan after a reload. */
const ACTIVE_SIG_KEY = "heliotrope:active_plan_sig";
const ACTIVE_OWNER_KEY = "heliotrope:active_owner";

/** Merge consecutive slot allocations into "11:30 PM–1:00 AM" style windows. */
function runWindows(allocs: Array<{ slot: number; timestamp: string }>, slotMin = 15): string[] {
  const sorted = [...allocs].sort((a, b) => a.slot - b.slot);
  const fmt = (iso: string, plusMin = 0) =>
    new Date(new Date(iso).getTime() + plusMin * 60_000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const out: string[] = [];
  let i = 0;
  while (i < sorted.length) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1].slot === sorted[j].slot + 1) j++;
    out.push(`${fmt(sorted[i].timestamp)}–${fmt(sorted[j].timestamp, slotMin)}`);
    i = j + 1;
  }
  return out;
}

/** Empty means "use the default"; garbage must not silently fall back. */
function parseCapacity(raw: string, fallback: number): { value?: number; error?: string } {
  const t = raw.trim();
  if (!t) return { value: fallback };
  const n = Number(t);
  if (!Number.isFinite(n) || n <= 0) return { error: "Enter a capacity greater than 0 kW." };
  if (n > 1000) return { error: "Enter a capacity up to 1000 kW." };
  return { value: n };
}

/** "06:00" + 2h -> "08:00" (wraps past midnight). */
function addHoursToClock(hhmm: string, hours: number): string {
  const [h, m] = hhmm.split(":").map(Number);
  const total = (((h || 0) * 60 + (m || 0) + Math.round(hours * 60)) % 1440 + 1440) % 1440;
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

/** Inline prompt for the one number a load still needs before it can be planned. */
function DetailPrompt({ need, onSave }: { need: Detail; onSave: (v: number) => void }) {
  const [val, setVal] = useState("");
  const n = Number(val);
  const ok = Number.isFinite(n) && n > 0 && (need === "energy" ? n <= 1000 : n <= 1440);
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (ok) onSave(need === "duration" ? Math.round(n) : n);
      }}
      className="mt-2.5 flex flex-wrap items-center gap-2"
    >
      <span className="font-mono text-[11px] text-amber-400/90">
        {need === "energy" ? "How much energy does it need in total?" : "How long does one run take?"}
      </span>
      <input
        value={val}
        onChange={(e) => setVal(e.target.value.replace(/[^0-9.]/g, ""))}
        inputMode="decimal"
        aria-label={need === "energy" ? "Energy needed in kilowatt-hours" : "Run length in minutes"}
        placeholder={need === "energy" ? "kWh" : "min"}
        className={`w-20 ${inputCls}`}
      />
      <button
        type="submit"
        disabled={!ok}
        className="cursor-pointer rounded-full bg-lime-300 px-4 py-2 text-[12px] font-medium text-black transition hover:bg-lime-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
      >
        Save
      </button>
    </form>
  );
}

function greeting(now = new Date()) {
  const h = now.getHours();
  return h < 5 ? "Late night" : h < 12 ? "Morning" : h < 18 ? "Afternoon" : "Evening";
}

// Each preset carries the one extra field its load type needs (the backend
// refuses to schedule an interruptible load without energy, or an atomic one
// without a run length), so a quick fill is always addable as-is.
const PRESETS = [
  { label: "EV charger", name: "EV charger", powerKw: "7.4", readyBy: "07:00", flex: 3, energyKwh: "20", durationMin: "" },
  { label: "Water heater", name: "Water heater", powerKw: "2", readyBy: "06:00", flex: 2, energyKwh: "", durationMin: "" },
  { label: "Washing machine", name: "Washing machine", powerKw: "2", readyBy: "18:00", flex: 4, energyKwh: "", durationMin: "60" },
  { label: "Borewell pump", name: "Borewell pump", powerKw: "1.5", readyBy: "08:00", flex: 3, energyKwh: "4", durationMin: "" },
];

export default function Dashboard() {
  const [auth] = useState(() => getFirebaseAuth());
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileLoaded, setProfileLoaded] = useState(false);
  const [jobs, setJobs] = useState<DashboardJob[]>([]);
  const [ranked, setRanked] = useState<RankedJob[] | null>(null);
  const [ranking, setRanking] = useState(false);
  const [rankNote, setRankNote] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [fName, setFName] = useState("");
  const [fPower, setFPower] = useState("");
  const [fReady, setFReady] = useState("06:00");
  const [fFlex, setFFlex] = useState(2);
  const [fEnergy, setFEnergy] = useState("");
  const [fDuration, setFDuration] = useState("");
  const [fTempMin, setFTempMin] = useState("");
  const [fTempMax, setFTempMax] = useState("");
  const [signal, setSignal] = useState<CarbonSignalResponse | null>(null);
  const [signalError, setSignalError] = useState<string | null>(null);
  const [forecast, setForecast] = useState<CarbonForecastResponse | null>(null);
  const [forecastMode, setForecastMode] = useState<ForecastMode>("ACTUAL");
  const [forecastError, setForecastError] = useState<string | null>(null);
  const [riskWeight, setRiskWeight] = useState<number>(0.5);
  // "waking" = the first health probe failed; free hosts need ~50s to wake up.
  const [backend, setBackend] = useState<"checking" | "waking" | "online" | "offline">("checking");
  const [backendTry, setBackendTry] = useState(0);
  // Which kind of load the name being typed is: decided by the backend (Jev when it has a
  // key, otherwise its built-in rules). Only when it can't be reached do we guess locally.
  const cls = useLoadClassification(fName, backend === "online");
  const activePreview = cls.result;
  const localGuess = useMemo(() => classifyJob(fName), [fName]);
  const guessedType = useMemo<JobType | undefined>(
    () =>
      cls.status === "unavailable"
        ? kindOf({ id: "", name: fName, kind: localGuess.category, powerKw: 0, readyBy: "", flexHours: 0, shiftable: localGuess.shiftable })
        : undefined,
    [cls.status, fName, localGuess]
  );
  const effectiveType: JobType | undefined = activePreview?.jobType ?? guessedType;
  const effectiveCategory = activePreview?.category ?? localGuess.category;
  const formFields = fieldsFor(effectiveType);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  const [signInError, setSignInError] = useState<string | null>(null);

  const [coordCapacity, setCoordCapacity] = useState("30");
  const [coordCapacityError, setCoordCapacityError] = useState<string | null>(null);
  const [coordResult, setCoordResult] = useState<CoordinationResult | null>(null);
  const [coordBusy, setCoordBusy] = useState(false);
  const [coordError, setCoordError] = useState<string | null>(null);

  // Backend-computed "run now" vs planned comparison for the current plan.
  const [impact, setImpact] = useState<CompareSchedulerResult | null>(null);
  const impactSeq = useRef(0);
  const [poolInfo, setPoolInfo] = useState<PoolSummary | null>(null);
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
    setProfileError(null);
    setLoadError(null);
    try {
      const snap = await getDoc(doc(db, "users", u.uid));
      setProfile(snap.exists() ? (snap.data() as Profile) : {});
      if (snap.exists()) void scrubLegacyEmail(db, u.uid, snap.data());
    } catch {
      // A failed read is NOT "new user": treating it that way would pop the
      // onboarding wizard over an existing account (offline, rules, quota).
      setProfileError("Couldn't load your profile. Check your connection and retry.");
      setProfileLoaded(true);
      return;
    }
    try {
      const js = await getDocs(collection(db, "users", u.uid, "jobs"));
      setJobs(js.docs.map((d) => ({ id: d.id, ...(d.data() as Omit<DashboardJob, "id">) })));
    } catch {
      setLoadError("Couldn't load your saved loads. They are safe; check your connection and retry.");
    }
    setProfileLoaded(true);
  }, []);

  const clearLiveSession = useCallback(() => {
    try {
      for (const k of [ACTIVE_SCHEDULE_KEY, ACTIVE_IMPACT_KEY, ACTIVE_SIG_KEY, ACTIVE_OWNER_KEY]) {
        localStorage.removeItem(k);
      }
    } catch {
      /* storage blocked: nothing persisted to clear */
    }
    setImpact(null);
    setPoolInfo(null);
    setPlannedJobsSignature(null);
    setLiveId(null);
    setLiveState(null);
    setLiveHistory(null);
  }, []);

  useEffect(() => {
    if (!auth) {
    // Firebase not configured: nothing will ever resolve, so stop showing "loading".
    // Deferred, and `ready` starts false on both server and client so they render the
    // same first frame (a different initial value caused a hydration mismatch).
    queueMicrotask(() => setReady(true));
    return;
    }
    return onAuthStateChanged(auth, (u) => {
      setUser(u);
      setReady(true);
      if (u) {
        void loadAll(u);
      } else {
        // Signed out: nothing of the previous account may linger in memory or storage.
        clearLiveSession();
        setJobs([]);
        setRanked(null);
        setCoordResult(null);
      }
    });
  }, [auth, loadAll, clearLiveSession]);

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

  // A 404 mid-session means the backend lost the schedule (restart, redeploy,
  // ephemeral SQLite). Drop it so actions and the tick stop hitting a dead id.
  const liveFailure = useCallback((e: unknown, fallback: string) => {
    if ((e as { status?: number }).status === 404) {
      localStorage.removeItem(ACTIVE_SCHEDULE_KEY);
      localStorage.removeItem(ACTIVE_IMPACT_KEY);
      localStorage.removeItem(ACTIVE_SIG_KEY);
      setImpact(null);
      setPlannedJobsSignature(null);
      setLiveId(null);
      setLiveState(null);
      setLiveHistory(null);
      setLiveError("This schedule is no longer on the server (the backend restarted). Plan again.");
      return;
    }
    setLiveError(e instanceof Error ? e.message : fallback);
  }, []);

  // Rehydrate a live session that survived a reload. A stale id (schedule
  // deleted, backend restarted, network down) is dropped quietly — the user
  // simply plans again.
  useEffect(() => {
    if (!user) return;
    const savedId = localStorage.getItem(ACTIVE_SCHEDULE_KEY);
    if (!savedId) return;
    if (localStorage.getItem(ACTIVE_OWNER_KEY) !== user.uid) {
      // A schedule saved by another account (or before owners were recorded).
      queueMicrotask(clearLiveSession);
      return;
    }
    // Deferred into a microtask so the effect body itself stays free of state
    // updates; the effect only kicks off the rehydration.
    void Promise.resolve()
      .then(() => refreshLive(savedId))
      .then(() => {
        setLiveId(savedId);
        try {
          setPlannedJobsSignature(localStorage.getItem(ACTIVE_SIG_KEY));
          const raw = localStorage.getItem(ACTIVE_IMPACT_KEY);
          const saved = raw ? (JSON.parse(raw) as { id?: string; impact?: CompareSchedulerResult }) : null;
          if (saved?.id === savedId && saved.impact) setImpact(saved.impact);
        } catch {
          /* unreadable: show the schedule without the impact card */
        }
      })
      .catch((e) => {
        // Only a definitive 404 drops the saved id; a network blip keeps it for the next load.
        if ((e as { status?: number }).status === 404) localStorage.removeItem(ACTIVE_SCHEDULE_KEY);
      });
  }, [user, refreshLive, clearLiveSession]);

  // Poll the live schedule forward once a minute. Each tick advances the
  // backend clock, then we re-read state — refresh only, no other side effects.
  useEffect(() => {
    if (!liveId) return;
    const t = setInterval(() => {
      void tickSchedule(liveId, new Date().toISOString())
        .then(() => refreshLive(liveId))
        .catch((e) => {
          // Transient failures retry on the next poll; a lost schedule stops it.
          if ((e as { status?: number }).status === 404) liveFailure(e, "");
        });
    }, 60_000);
    return () => clearInterval(t);
  }, [liveId, refreshLive, liveFailure]);

  // Loads that cannot be planned yet because a number only the user knows is missing.
  const needsDetail = useMemo(() => buildSpecs(jobs as StoredJob[]).needsDetail, [jobs]);

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
    const { specs, needsDetail: left } = buildSpecs(jobs as StoredJob[]);
    if (!specs.length) {
      setLiveError(
        left.length
          ? `Add the missing detail for ${left.map((n) => n.job.name).join(", ")} first (see your loads above).`
          : "Add a load first."
      );
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

      const planBody = {
        jobs: specs,
        capacity_kw: cap.value,
        scheduler: "CPSAT",
        ...(carbonPayload ? { carbon: carbonPayload } : {}),
      };
      setImpact(null);
      const seq = ++impactSeq.current;
      // Replacing our own plan: the backend cancels it so it doesn't count as someone else's load.
      const state = await planSchedule({ ...planBody, ...(liveId ? { replaces_schedule_id: liveId } : {}) });
      setPoolInfo(state.pool ?? null);
      // Fire-and-forget: the impact card is a bonus and must never block or fail the plan.
      // It shares the same pool (minus our new plan) so its numbers match what was optimized.
      void compareSchedulers({
        ...planBody,
        schedulers: ["ASAP", "CPSAT"],
        share_pool: true,
        pool_exclude_schedule_id: state.schedule_id,
      })
        .then((cmp) => {
          // Only a real solution carries honest savings; INFEASIBLE/UNKNOWN shows no impact card.
          const raw = cmp.results.CPSAT ?? null;
          const result = raw && (raw.status === "OPTIMAL" || raw.status === "FEASIBLE") ? raw : null;
          if (impactSeq.current !== seq) return; // a newer plan superseded this one
          setImpact(result);
          try {
            if (result) localStorage.setItem(ACTIVE_IMPACT_KEY, JSON.stringify({ id: state.schedule_id, impact: result }));
          } catch {
            /* storage full/blocked: the card just won't survive a reload */
          }
        })
        .catch(() => {
          if (impactSeq.current === seq) setImpact(null);
        });
      setLiveId(state.schedule_id);
      localStorage.setItem(ACTIVE_SCHEDULE_KEY, state.schedule_id);
      try {
        if (user) localStorage.setItem(ACTIVE_OWNER_KEY, user.uid);
        localStorage.setItem(ACTIVE_SIG_KEY, jobsSignature);
      } catch {
        /* storage blocked: stale detection just won't survive a reload */
      }
      setPlannedJobsSignature(jobsSignature);
      await refreshLive(state.schedule_id);
      if (left.length) setLiveError(`Left out until you add their details: ${left.map((n) => n.job.name).join(", ")}.`);
    } catch (e) {
      setLiveError(e instanceof Error ? e.message : "Planning failed.");
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
      liveFailure(e, "Replan failed.");
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
      liveFailure(e, "Event failed.");
    } finally {
      setLiveBusy(false);
    }
  }

  async function liveOverride(jobId: string, command: OverrideCommand, newDeadlineAt?: string) {
    if (!liveId) return;
    setLiveBusy(true);
    setLiveError(null);
    try {
      await overrideSchedule(liveId, {
        job_id: jobId,
        command,
        ...(newDeadlineAt ? { new_deadline_at: newDeadlineAt } : {}),
      });
      await refreshLive(liveId);
    } catch (e) {
      liveFailure(e, "That change couldn't be applied.");
    } finally {
      setLiveBusy(false);
    }
  }

  // Schedules all of the user's loads together under one site limit. Each load is
  // its own participant, so the fairness and peak logic are real; there is no demo data.
  const runCoordination = useCallback(async () => {
    const parsed = parseCapacity(coordCapacity, 30);
    if (parsed.error || parsed.value === undefined) {
      setCoordCapacityError(parsed.error ?? "Enter a capacity greater than 0 kW.");
      return;
    }
    setCoordCapacityError(null);
    const { specs } = buildSpecs(jobs as StoredJob[]);
    if (specs.length === 0) {
      setCoordResult(null);
      return;
    }
    setCoordBusy(true);
    setCoordError(null);
    try {
      const res = await coordinateBuilding({
        participants: specs.map((sp) => ({ id: sp.id, name: sp.normalized_name })),
        shared_resource: { capacity_kw: parsed.value },
        jobs: specs.map((sp) => ({ ...sp, participant_id: sp.id })),
      });
      setCoordResult(res);
    } catch (err) {
      setCoordError(err instanceof Error ? err.message : "Coordination failed.");
    } finally {
      setCoordBusy(false);
    }
  }, [coordCapacity, jobs]);

  // Wait for the backend (it may be waking from sleep), THEN read the grid
  // signal. Fetching immediately would fail on a healthy-but-cold deployment.
  useEffect(() => {
    const ctl = new AbortController();
    void waitForBackend({
      signal: ctl.signal,
      onWaking: () => setBackend("waking"),
    }).then((up) => {
      if (ctl.signal.aborted) return;
      if (!up) {
        setBackend("offline");
        setSignalError("The compute backend isn't answering.");
        return;
      }
      setBackend("online");
      // Six hours of recent past plus the next 24: the window the planner actually works in.
      const start = new Date(Date.now() - 6 * 3600 * 1000);
      const end = new Date(Date.now() + 24 * 3600 * 1000);
      getCarbonSignal({ start: start.toISOString(), end: end.toISOString() })
        .then((s) => {
          if (!ctl.signal.aborted) setSignal(s);
        })
        .catch(() => {
          if (!ctl.signal.aborted) setSignalError("Couldn't read the grid carbon signal.");
        });

      const fStart = new Date();
      const fEnd = new Date(fStart.getTime() + 24 * 3600 * 1000);
      getCarbonForecast({ start: fStart.toISOString(), end: fEnd.toISOString() })
        .then((f) => {
          if (ctl.signal.aborted) return;
          setForecast(f);
          setForecastError(null);
        })
        .catch(() => {
          if (ctl.signal.aborted) return;
          setForecast(null);
          setForecastError("Forecast unavailable — showing actual signal only.");
        });
    });
    return () => ctl.abort();
  }, [backendTry]);

  function retryBackend() {
    setBackend("checking");
    setSignalError(null);
    setBackendTry((n) => n + 1);
  }

  // Keep the shared-capacity view in step with the loads and the limit (it is cheap
  // on the backend), once the backend is reachable. Debounced while typing a limit.
  useEffect(() => {
    if (!profileLoaded || backend !== "online") return;
    const t = setTimeout(() => void runCoordination(), 500);
    return () => clearTimeout(t);
  }, [profileLoaded, backend, runCoordination]);

  // Validate the in-progress load against the backend before adding, so
  // feasibility errors/warnings show inline in the form (never silent). It waits until
  // we know what kind of load this is, so it never judges a guess made mid-typing.
  const classifyStatus = cls.status;
  useEffect(() => {
    const name = fName.trim();
    if (name.length < 2 || !fReady || !effectiveType) {
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
      const pKw = Number(fPower);
      const hasPower = Number.isFinite(pKw) && pKw > 0;
      const eKwh = formFields.energy && fEnergy ? Number(fEnergy) : null;
      const dMin = formFields.duration && fDuration ? Math.round(Number(fDuration)) : null;
      const now = nextSlot();
      const isThermalForm = effectiveType === "THERMAL";
      const isAcForm = AC_RE.test(`${name} ${effectiveCategory}`);
      const tMin = fTempMin ? Number(fTempMin) : isThermalForm ? (isAcForm ? 22 : 40) : NaN;
      const tMax = fTempMax ? Number(fTempMax) : isThermalForm ? (isAcForm ? 26 : 65) : NaN;
      const spec: LoadSpec = {
        id: "form-preview",
        normalized_name: name,
        category: effectiveCategory,
        job_type: effectiveType,
        power_kw: hasPower ? pKw : null,
        max_power_kw: hasPower ? pKw : null,
        duration_minutes: dMin,
        energy_required_kwh: eKwh,
        min_chunk_minutes: effectiveType === "DEFERRABLE_INTERRUPTIBLE" ? 15 : null,
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
                max_power_kw: hasPower ? pKw : 2.0,
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
                max_power_kw: hasPower ? pKw : 2.0,
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
        deadline_at: latestFinish({ readyBy: fReady, flexHours: fFlex }, now).toISOString(),
        assumptions: [],
      };
      validateLoad(spec)
        .then((r) => {
          if (cancelled) return;
          setAddErrors(r.errors.map((e) => e.message));
          setAddWarnings(r.warnings.map((w) => w.message));
        })
        .catch((e) => {
          if (cancelled) return;
          const status = (e as { status?: number }).status;
          if (status !== undefined && status >= 400 && status < 500) {
            // The backend rejected the load itself: surface why instead of saving it.
            setAddErrors([e instanceof Error ? e.message : "This load was rejected."]);
          } else {
            // Network/server trouble: validation is advisory, the form still works offline.
            setAddErrors([]);
          }
          setAddWarnings([]);
        });
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
    // classifyStatus is listed so a settled answer always triggers a fresh check.
  }, [fName, fReady, fFlex, fPower, fEnergy, fDuration, fTempMin, fTempMax, activePreview, effectiveType, effectiveCategory, formFields.energy, formFields.duration, classifyStatus]);

  async function addJob() {
    if (!user || !fName.trim() || !fReady) return;
    // Wait for the answer on what kind of load this is, so we save Jev's verdict and not a guess.
    if (cls.status === "loading" || cls.status === "idle") return;
    // Backend feasibility errors block the add; warnings stay advisory.
    // (Offline the validator stays silent, so addErrors is empty and the add proceeds.)
    if (addErrors.length > 0) return;
    const powerKw = Number(fPower);
    if (!fPower.trim() || !Number.isFinite(powerKw) || powerKw <= 0) {
      setFPowerError("Enter a power greater than 0 kW.");
      return;
    }
    setFPowerError(null);
    const db = getDb();
    if (!db) return;
    setAddError(null);
    setAdding(true);
    // The backend's answer is what gets saved. Only when it couldn't be reached do we fall
    // back to the local guess for the category and the movable/always-on flag, and then no
    // load type is stored: it is worked out from the name when planning.
    const jobType = activePreview?.jobType;
    const shiftable = jobType ? jobType !== "FIXED" : localGuess.shiftable;
    const kind = effectiveCategory;
    // Only keep the numbers this kind of load actually asks for, never a leftover from a previous name.
    const energyKwh = formFields.energy && fEnergy ? Number(fEnergy) : undefined;
    const durationMin = formFields.duration && fDuration ? Math.round(Number(fDuration)) : undefined;

    const isThermal = effectiveType === "THERMAL";
    const isAc = AC_RE.test(`${fName} ${kind}`);
    const tempMinC = isThermal ? (fTempMin ? Number(fTempMin) : isAc ? 22 : 40) : undefined;
    const tempMaxC = isThermal ? (fTempMax ? Number(fTempMax) : isAc ? 26 : 65) : undefined;

    try {
      const ref = await addDoc(collection(db, "users", user.uid, "jobs"), {
        name: fName.trim(),
        kind,
        shiftable,
        powerKw,
        readyBy: fReady,
        flexHours: fFlex,
        // Written only when known, so a missing value stays missing instead of becoming a misleading zero.
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
          kind,
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
      setFPower("");
      setFEnergy("");
      setFDuration("");
      setFTempMin("");
      setFTempMax("");
      setAddErrors([]);
      setAddWarnings([]);
      setFPowerError(null);
    } catch (e) {
      const code = e instanceof Error && "code" in e ? (e as { code?: string }).code : undefined;
      setAddError(
        code === "permission-denied"
          ? "Saving was blocked by the database security rules."
          : "Couldn't save this load. Check your connection and try again."
      );
    } finally {
      setAdding(false);
    }
  }

  function applyPreset(p: (typeof PRESETS)[number]) {
    setFName(p.name);
    setFPower(p.powerKw);
    setFReady(p.readyBy);
    setFFlex(p.flex);
    setFEnergy(p.energyKwh);
    setFDuration(p.durationMin);
    setFPowerError(null);
    setAddError(null);
  }

  /** Rank by priority: Jev judges how essential each appliance is, the backend combines
   *  that with time pressure, size and rigidity. Falls back to the local heuristic, and
   *  says so, if the backend is unreachable. */
  async function toggleRanking() {
    if (ranked) {
      setRanked(null);
      setRankNote(null);
      return;
    }
    const shiftable = jobs.filter((j) => j.shiftable !== false);
    setRanking(true);
    try {
      const res = await prioritizeLoads(
        shiftable.map((j) => ({
          id: j.id,
          name: j.name,
          kind: j.kind,
          power_kw: j.powerKw,
          hours_until_ready: hoursUntilReady(j.readyBy),
          flex_hours: j.flexHours,
        }))
      );
      const byId = new Map(shiftable.map((j) => [j.id, j]));
      setRanked(
        res.items.flatMap((it) => {
          const j = byId.get(it.id);
          return j ? [{ ...j, score: it.score, band: it.band, reason: it.reason, source: it.source }] : [];
        })
      );
      setRankNote(
        res.provider === "jev"
          ? `Ranked with Jev (importance) plus time pressure, size and flexibility.${res.notes[0] ? ` ${res.notes[0]}` : ""}`
          : res.notes[0] ?? "Ranked with the built-in heuristic."
      );
    } catch {
      setRanked(jevRank(jobs));
      setRankNote("Backend unreachable: ranked with the local heuristic.");
    } finally {
      setRanking(false);
    }
  }

  async function saveDetail(id: string, need: Detail, value: number) {
    if (!user) return;
    const db = getDb();
    if (!db) return;
    const field = need === "energy" ? "energyKwh" : "durationMin";
    try {
      await updateDoc(doc(db, "users", user.uid, "jobs", id), { [field]: value });
      setJobs((js) => js.map((j) => (j.id === id ? { ...j, [field]: value } : j)));
      setRanked((r) => (r ? r.map((j) => (j.id === id ? { ...j, [field]: value } : j)) : r));
      setLoadError(null);
    } catch {
      setLoadError("Couldn't save that detail. Check your connection and try again.");
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
          {!auth && (
            <p className="mx-auto mt-4 max-w-sm text-sm leading-6 text-orange-300">
              Sign-in isn&apos;t configured on this deployment (the Firebase keys are missing).
            </p>
          )}
          <button
            onClick={async () => {
              setBusy(true);
              setSignInError(null);
              try {
                const a = getFirebaseAuth();
                if (a) await signInWithPopup(a, getGoogleProvider());
              } catch (e) {
                const code = e instanceof Error && "code" in e ? (e as { code?: string }).code : undefined;
                // Closing the popup is a choice, not an error worth shouting about.
                if (code !== "auth/popup-closed-by-user" && code !== "auth/cancelled-popup-request") {
                  setSignInError(
                    code === "auth/popup-blocked"
                      ? "Your browser blocked the sign-in popup. Allow popups for this site and retry."
                      : "Sign-in failed. Try again."
                  );
                }
              } finally {
                setBusy(false);
              }
            }}
            disabled={busy || !auth}
            className="mt-7 flex cursor-pointer items-center justify-center gap-2 rounded-full bg-white px-7 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-70"
          >
            {busy && <span className="spinner" />}
            {busy ? "Signing in…" : "Sign in with Google"}
          </button>
          {signInError && <p role="alert" className="mt-3 font-mono text-[12px] text-orange-300">{signInError}</p>}
          <p className="mt-6 text-[13px]">
            <Link href="/" className="text-zinc-500 transition hover:text-white">← back home</Link>
          </p>
        </div>
      </main>
    );
  }

  if (profileError) {
    return (
      <main className="grid min-h-screen place-items-center bg-black px-6 text-center text-zinc-100">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">We couldn&apos;t load your account</h1>
          <p className="mx-auto mt-3 max-w-sm text-sm leading-6 text-zinc-500">{profileError}</p>
          <button
            onClick={() => void loadAll(user)}
            className="mt-6 cursor-pointer rounded-full bg-white px-7 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98]"
          >
            Retry
          </button>
        </div>
      </main>
    );
  }

  const needsOnboarding = profileLoaded && (!profile || !profile.onboarded);
  const nameOf: Record<string, string> = Object.fromEntries(jobs.map((j) => [j.id, j.name]));
  const shiftableCount = jobs.filter((j) => j.shiftable !== false).length;

  return (
    <main className="min-h-screen bg-black text-zinc-100">
      {needsOnboarding && <Onboarding user={user} onDone={() => void loadAll(user)} />}
      {/* inert: while the wizard is open the page behind it must not take focus or be read out */}
      <div inert={needsOnboarding} className="mx-auto max-w-7xl px-4 py-4 sm:px-10 sm:py-6 lg:px-16">
        <header className="flex items-center justify-between">
          <Link href="/" className="inline-flex min-h-11 items-center text-[13px] font-semibold uppercase tracking-[0.28em]">Heliotrope</Link>
          <div className="relative flex items-center gap-3">
            <span
              role="status"
              title={
                backend === "online"
                  ? "Compute backend is online"
                  : backend === "offline"
                    ? "Compute backend isn't answering"
                    : "Connecting to the compute backend"
              }
              className="hidden items-center gap-2 font-mono text-[11px] text-zinc-500 sm:flex"
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  backend === "online" ? "bg-lime-300" : backend === "offline" ? "bg-red-400" : "animate-pulse bg-amber-400"
                }`}
              />
              {backend === "online" ? "backend online" : backend === "offline" ? "backend offline" : backend === "waking" ? "waking backend…" : "connecting…"}
            </span>
            {user.photoURL ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={user.photoURL} alt="" referrerPolicy="no-referrer" className="h-8 w-8 rounded-full bg-white/10 object-cover" />
            ) : null}
            <span className="max-w-40 truncate text-[13px] text-zinc-300">{user.displayName ?? user.email}</span>
            <button
              onClick={() => setMenuOpen((v) => !v)}
              aria-label="Account menu"
              className="grid h-11 w-11 cursor-pointer place-items-center text-lg leading-none text-zinc-300 transition hover:text-white active:scale-95"
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
          {greeting()}, {(user.displayName ?? "there").split(" ")[0]}.
        </h1>
        <p className="mt-3 max-w-lg text-[15px] leading-7 text-zinc-500">
          {profile?.place ? `${profile.place}${profile.rooms ? ` · ${profile.rooms} rooms` : ""} — ` : ""}
          add your loads, then plan them into the cleanest hours of the grid.
        </p>

        {(backend === "waking" || backend === "offline") && (
          <div role="status" className="mt-6 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-400/25 bg-amber-400/[0.04] px-5 py-4">
            <p className="text-[13px] leading-6 text-amber-200/90">
              {backend === "waking"
                ? "Waking the compute backend — this can take up to a minute on the first visit. Planning and charts unlock as soon as it answers."
                : "The compute backend isn't answering, so charts and planning are unavailable."}
            </p>
            {backend === "offline" && (
              <button onClick={retryBackend} className="cursor-pointer rounded-full border border-amber-400/40 px-4 py-1.5 text-[12px] text-amber-200 transition hover:bg-amber-400/10 active:scale-[0.97]">
                Retry
              </button>
            )}
          </div>
        )}

        {/* where you are in the flow */}
        <ol className="mt-8 grid gap-2 sm:grid-cols-3" aria-label="Progress">
          {[
            { n: 1, title: "Add loads", done: jobs.length > 0, note: jobs.length ? `${jobs.length} added` : "name what you run" },
            { n: 2, title: "Plan", done: liveState !== null, note: liveState ? `v${liveState.version} · ${liveState.solver_status.toLowerCase()}` : shiftableCount ? "ready to plan" : "needs a flexible load" },
            { n: 3, title: "Track", done: liveState !== null && liveState.jobs.some((j) => j.status !== "PENDING"), note: "start, finish, replan" },
          ].map((st) => (
            <li key={st.n} className={`flex items-center gap-3 rounded-2xl border px-4 py-3 ${st.done ? "border-lime-300/30 bg-lime-300/[0.04]" : "border-white/10"}`}>
              <span className={`grid h-6 w-6 shrink-0 place-items-center rounded-full font-mono text-[11px] ${st.done ? "bg-lime-300 text-black" : "border border-white/15 text-zinc-500"}`}>
                {st.done ? "✓" : st.n}
              </span>
              <span className="min-w-0">
                <span className="block text-[13px] font-medium">{st.title}</span>
                <span className="block truncate font-mono text-[11px] text-zinc-600">{st.note}</span>
              </span>
            </li>
          ))}
        </ol>

        {/* loads */}
        <section className="mt-3 overflow-hidden rounded-2xl border border-white/10 bg-[#0a0a0a]">
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-5 sm:px-8">
            <div>
              <h2 className="text-[15px] font-medium">Your loads</h2>
              <p className="mt-0.5 font-mono text-[11px] text-zinc-600">
                {jobs.length === 0 ? "nothing added yet" : `${jobs.length} load${jobs.length > 1 ? "s" : ""}${ranked ? " · ranked" : ""}`}
              </p>
            </div>
            <button
              onClick={() => void toggleRanking()}
              disabled={jobs.length === 0 || ranking}
              title="Which load matters first: Jev judges how essential each appliance is; time pressure, size and flexibility are added. The real timing comes from Plan live below."
              className="min-h-11 cursor-pointer rounded-full border border-white/15 px-5 py-2 text-[13px] text-zinc-300 transition hover:border-white/40 hover:text-white active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
            >
              {ranking ? "Ranking…" : ranked ? "Hide ranking" : "Rank by priority"}
            </button>
          </div>

          {ranked && rankNote && (
            <p className="border-t border-white/5 px-4 py-3 font-mono text-[11px] text-zinc-500 sm:px-8">{rankNote}</p>
          )}
          {loadError && (
            <div role="alert" className="flex flex-wrap items-center justify-between gap-3 border-t border-white/5 px-6 py-4 sm:px-8">
              <p className="font-mono text-[12px] text-orange-300">{loadError}</p>
              <button onClick={() => void loadAll(user)} className="cursor-pointer rounded-full border border-white/15 px-4 py-1.5 text-[12px] text-zinc-300 transition hover:border-white/40 hover:text-white active:scale-[0.97]">
                Retry
              </button>
            </div>
          )}

          {(ranked ? [...ranked, ...jobs.filter((j) => j.shiftable === false)] : jobs).map((item, i) => {
            const j = item as DashboardJob & Partial<RankedJob>;
            return (
              <div key={j.id} className="group flex items-start gap-3 border-t border-white/5 px-4 py-4 transition hover:bg-white/[0.02] sm:items-center sm:gap-5 sm:px-8 sm:py-5">
                {ranked && j.shiftable !== false && <span className="w-6 shrink-0 font-mono text-[13px] text-zinc-600">{String(i + 1).padStart(2, "0")}</span>}
                <KindIcon kind={j.kind} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <p className="text-[15px] font-medium">{j.name}</p>
                    {j.shiftable === false ? (
                      <span className="shrink-0 rounded-full bg-white/5 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">always-on · filtered</span>
                    ) : (
                      ranked && j.band && (
                        <>
                          <BandChip band={j.band} />
                          {j.source === "jev" && (
                            <span title="Jev judged how essential this appliance is" className="shrink-0 rounded-full bg-violet-400/15 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-violet-300">
                              jev
                            </span>
                          )}
                        </>
                      )
                    )}
                  </div>
                  <p className="mt-1.5 text-[13px] text-zinc-500">
                    {j.kind || "Load"} · {loadTypeLabel(kindOf(j as StoredJob))} · {j.powerKw} kW · ready by {j.readyBy}
                    {j.flexHours > 0 ? ` (up to ${addHoursToClock(j.readyBy, j.flexHours)})` : ""}
                    {j.energyKwh ? ` · ${j.energyKwh} kWh` : ""}
                    {j.durationMin ? ` · ${j.durationMin} min` : ""}
                    {j.tempMinC !== undefined && j.tempMaxC !== undefined ? ` · comfort ${j.tempMinC}°C–${j.tempMaxC}°C` : ""}
                  </p>
                  {(() => {
                    const need = detailNeeded(j as StoredJob);
                    return need ? <DetailPrompt need={need} onSave={(v) => void saveDetail(j.id, need, v)} /> : null;
                  })()}
                  {ranked && j.shiftable !== false && (
                    <>
                      <div className="mt-2.5 h-1 max-w-md overflow-hidden rounded-full bg-white/10">
                        <div className="h-full rounded-full bg-lime-300 transition-[width] duration-700" style={{ width: `${j.score}%` }} />
                      </div>
                      <p className="mt-1.5 text-[13px] text-zinc-500">{j.reason}</p>
                    </>
                  )}
                </div>
                <button onClick={() => void removeJob(j.id)} aria-label={`Remove ${j.name}`} className="-mr-2 shrink-0 cursor-pointer px-2 py-2.5 font-mono text-[12px] text-zinc-500 transition hover:text-red-300 focus-visible:opacity-100 focus-visible:text-red-300 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100">
                  remove
                </button>
              </div>
            );
          })}

          {jobs.length === 0 && (
            <p className="border-t border-white/5 px-4 py-6 text-sm leading-6 text-zinc-500 sm:px-8">
              No loads yet. Name your first one below — it takes ten seconds.
            </p>
          )}

          {/* add */}
          <div className="border-t border-white/10 bg-white/[0.015] px-4 py-6 sm:px-8">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Add a load — name anything</p>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11px] text-zinc-600">quick examples</span>
              {PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => applyPreset(p)}
                  className="min-h-10 cursor-pointer rounded-full border border-white/15 px-3.5 py-2 text-[12px] text-zinc-400 transition hover:border-lime-300/60 hover:text-white active:scale-[0.96]"
                >
                  {p.label}
                </button>
              ))}
            </div>
            <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
              <input aria-label="Load name" value={fName} onChange={(e) => setFName(e.target.value)} maxLength={40} placeholder="Name — e.g. hostel borewell pump" className={`w-full ${inputCls}`} />
              <div
                aria-live="polite"
                className={`flex min-h-[2.75rem] w-full items-center gap-2 overflow-hidden px-4 py-2 ${inputCls} ${cls.status === "idle" ? "opacity-50" : ""}`}
              >
                {cls.status === "loading" ? (
                  <span className="spinner shrink-0" aria-hidden="true" />
                ) : (
                  <span
                    aria-hidden="true"
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                      cls.status === "idle" ? "bg-zinc-700" : effectiveType && effectiveType !== "FIXED" ? "bg-lime-300" : "bg-zinc-600"
                    }`}
                  />
                )}
                <span
                  className="min-w-0 flex-1 text-sm leading-5 text-zinc-200"
                  title={
                    activePreview
                      ? activePreview.provider === "jev"
                        ? `Category: ${activePreview.category}. Decided by Jev.`
                        : activePreview.fallbackReason
                          ? `Category: ${activePreview.category}. Jev couldn't answer (${activePreview.fallbackReason}), so the built-in rules did.`
                          : `Category: ${activePreview.category}. Decided by the built-in rules.`
                      : undefined
                  }
                >
                  {cls.status === "idle"
                    ? "Type a name and we'll work out what kind of load it is."
                    : cls.status === "loading"
                      ? "Working out what kind of load this is…"
                      : activePreview
                        ? `${
                            activePreview.provider === "jev"
                              ? "Jev"
                              : activePreview.fallbackReason
                                ? "Built-in rules (Jev unavailable)"
                                : "Built-in rules"
                          }: ${loadTypeLabel(activePreview.jobType)} · ${Math.round(activePreview.confidence * 100)}% sure`
                        : `${backend === "online" ? "Couldn't reach the classifier" : "The planner isn't reachable yet"}, so this is only a rough guess from the name: ${loadTypeLabel(guessedType)}.${backend === "online" ? " Edit the name to try again." : ""}`}
                </span>
                {activePreview && (
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
                      activePreview.provider === "jev" ? "bg-violet-400/15 text-violet-300" : "bg-white/5 text-zinc-500"
                    }`}
                  >
                    {activePreview.provider === "jev" ? "Jev" : "rules"}
                  </span>
                )}
                {cls.status === "unavailable" && (
                  <span className="shrink-0 rounded-full bg-amber-400/10 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-amber-300/90">guess</span>
                )}
              </div>
            </div>
            <div className="mt-3 flex flex-wrap items-start gap-x-5 gap-y-3">
              <label className="flex flex-col gap-1 text-[13px] text-zinc-400">
                <span className="flex items-center gap-2">
                  <input value={fPower} onChange={(e) => { setFPower(e.target.value.replace(/[^0-9.]/g, "")); if (fPowerError) setFPowerError(null); }} inputMode="decimal" placeholder="kW" className={`w-20 ${inputCls}`} />
                  <span className="font-mono text-[11px] text-zinc-600">power it draws (kW)</span>
                </span>
                {fPowerError && (
                  <span className="font-mono text-[11px] text-orange-300">{fPowerError}</span>
                )}
              </label>
              <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                <span className="font-mono text-[11px] text-zinc-600">needed by</span>
                <input type="time" value={fReady} onChange={(e) => setFReady(e.target.value)} className={`cursor-pointer [color-scheme:dark] ${inputCls}`} />
              </label>
              <label className="flex flex-wrap items-center gap-2 text-[13px] text-zinc-400">
                <span className="font-mono text-[11px] text-zinc-600">
                  {fFlex === 0 ? "must finish on time" : `can finish up to ${fFlex} h late`}
                </span>
                <input type="range" min={0} max={6} value={fFlex} aria-label="Hours it can finish late" onChange={(e) => setFFlex(Number(e.target.value))} className="w-28 cursor-pointer accent-lime-300" />
              </label>
              <button
                onClick={() => void addJob()}
                disabled={adding || !fName.trim() || !fReady || addErrors.length > 0 || cls.status === "loading" || cls.status === "idle"}
                title={
                  addErrors.length > 0
                    ? "Fix the problem below before adding."
                    : cls.status === "loading"
                      ? "Waiting to hear what kind of load this is."
                      : undefined
                }
                className="flex min-h-11 cursor-pointer items-center gap-2 rounded-full bg-white px-6 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
              >
                {adding && <span className="spinner" />}
                {adding ? "Adding…" : "Add load"}
              </button>
            </div>
            <p className="mt-2 font-mono text-[11px] leading-5 text-zinc-700">
              kW is how much power it uses while running; it is usually printed on the appliance label.
            </p>

            {/* Progressive disclosure: only the fields this kind of load actually needs. */}
            {(formFields.energy || formFields.duration || formFields.thermal || formFields.note) && (
              <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-3 border-t border-white/5 pt-4">
                {formFields.energy && (
                  <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                    <input
                      value={fEnergy}
                      onChange={(e) => setFEnergy(e.target.value.replace(/[^0-9.]/g, ""))}
                      inputMode="decimal"
                      placeholder="kWh"
                      className={`w-20 ${inputCls}`}
                    />
                    <span className="font-mono text-[11px] text-zinc-600">electricity it needs in total (kWh)</span>
                  </label>
                )}
                {formFields.duration && (
                  <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                    <input
                      value={fDuration}
                      onChange={(e) => setFDuration(e.target.value.replace(/[^0-9]/g, ""))}
                      inputMode="numeric"
                      placeholder="min"
                      className={`w-20 ${inputCls}`}
                    />
                    <span className="font-mono text-[11px] text-zinc-600">how long one run takes (minutes)</span>
                  </label>
                )}
                {formFields.thermal && (
                  <>
                    <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                      <input
                        value={fTempMin}
                        onChange={(e) => setFTempMin(e.target.value.replace(/[^0-9.]/g, ""))}
                        inputMode="decimal"
                        placeholder={AC_RE.test(`${fName} ${effectiveCategory}`) ? "22" : "40"}
                        className={`w-20 ${inputCls}`}
                      />
                      <span className="font-mono text-[11px] text-zinc-600">lowest comfortable temp (°C)</span>
                    </label>
                    <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                      <input
                        value={fTempMax}
                        onChange={(e) => setFTempMax(e.target.value.replace(/[^0-9.]/g, ""))}
                        inputMode="decimal"
                        placeholder={AC_RE.test(`${fName} ${effectiveCategory}`) ? "26" : "65"}
                        className={`w-20 ${inputCls}`}
                      />
                      <span className="font-mono text-[11px] text-zinc-600">highest comfortable temp (°C)</span>
                    </label>
                  </>
                )}
                {formFields.note && <span className="font-mono text-[11px] text-zinc-600">{formFields.note}</span>}
                {activePreview?.ambiguous && (
                  <span className="font-mono text-[11px] text-amber-400/80">
                    Might be something else ({Math.round(activePreview.confidence * 100)}% sure). Check the details.
                  </span>
                )}
              </div>
            )}
            {addError && <p role="alert" className="mt-3 font-mono text-[12px] text-orange-300">{addError}</p>}
            {addErrors.length > 0 && (
              <p className="mt-3 font-mono text-[11px] text-zinc-500">
                Fix the problem below to enable “Add load”.
              </p>
            )}
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

        <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-4 sm:p-8">
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

        <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-4 sm:p-8">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[15px] font-medium">Live schedule</h2>
              <p className="mt-0.5 font-mono text-[11px] text-zinc-600">
                {liveState ? `solver ${liveState.solver_status.toLowerCase()} · refreshes every minute` : "plans your loads on the backend, then tracks execution"}
              </p>
              {liveState && poolInfo?.applied && poolInfo.active_schedules > 0 && (
                <p className="mt-0.5 font-mono text-[11px] text-sky-300/80">
                  spread around {poolInfo.active_schedules} other active plan{poolInfo.active_schedules === 1 ? "" : "s"} · their peak{" "}
                  {poolInfo.peak_pooled_kw.toFixed(1)} kW
                </p>
              )}
            </div>
            <div className="flex items-start gap-2">
              <label className="flex flex-col gap-1 text-[13px] text-zinc-400">
                <span className="flex items-center gap-2">
                  <input value={liveCapacity} aria-label="Site capacity limit in kilowatts" onChange={(e) => setLiveCapacity(e.target.value.replace(/[^0-9.]/g, ""))} inputMode="decimal" className={`w-20 ${inputCls}`} />
                  <span className="font-mono text-[11px] text-zinc-600">kW site limit</span>
                </span>
                {liveCapacityError && (
                  <span className="font-mono text-[11px] text-orange-300">{liveCapacityError}</span>
                )}
              </label>
              <button
                onClick={() => void planLive()}
                disabled={liveBusy || jobs.length === 0 || backend !== "online"}
                title={
                  jobs.length === 0
                    ? "Add a load first."
                    : backend !== "online"
                      ? "Waiting for the compute backend."
                      : undefined
                }
                className={`min-h-11 cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-black transition active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30 ${
                  !liveId || loadsStale ? "bg-lime-300 hover:bg-lime-200" : "bg-white hover:bg-zinc-200"
                }`}
              >
                {liveBusy ? "Planning…" : liveId ? "Re-plan from my loads" : "Plan live"}
              </button>
            </div>
          </div>
          {liveError && <p aria-live="polite" className="mt-3 font-mono text-[12px] text-orange-300">{liveError}</p>}
          {!liveState && !liveError && (
            <p className="mt-3 text-[13px] leading-6 text-zinc-500">
              {jobs.length === 0
                ? "Add a load above, then plan it here."
                : needsDetail.length > 0
                  ? `${needsDetail.length} load${needsDetail.length > 1 ? "s" : ""} still need${needsDetail.length > 1 ? "" : "s"} a detail (see your loads above) and will be left out of the plan.`
                  : "Plan live solves the cleanest start time for every flexible load while keeping each deadline, then lets you track and replan as the day moves."}
            </p>
          )}
          {loadsStale && liveState && (
            <p aria-live="polite" className="mt-3 font-mono text-[12px] text-amber-400/90">
              Your loads changed since v{liveState.version}. Press “Re-plan from my loads” to include the changes.
            </p>
          )}
          {liveState && impact && (
            <div className="mt-4 rounded-2xl border border-lime-300/20 bg-lime-300/[0.03] p-5">
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div>
                  <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-lime-300/80">
                    Impact vs running everything now
                    {liveState.version > 1 ? " · as first planned (v1)" : ""}
                  </p>
                  {impact.metrics.co2_saved_percent !== null && impact.metrics.co2_saved_percent > 0.05 ? (
                    <p className="mt-1.5 text-3xl font-semibold tracking-tight">
                      −{impact.metrics.co2_saved_percent.toFixed(0)}% CO₂
                      <span className="ml-2 font-mono text-[13px] font-normal text-zinc-500">
                        {impact.metrics.co2_saved_kg?.toFixed(2)} kg saved of {((impact.metrics.total_co2_kg ?? 0) + (impact.metrics.co2_saved_kg ?? 0)).toFixed(2)} kg
                      </span>
                    </p>
                  ) : (
                    <p className="mt-1.5 text-[15px] text-zinc-300">
                      Starting now is already the cleanest way to meet your deadlines, so nothing was shifted.
                    </p>
                  )}
                </div>
                <p className="font-mono text-[11px] text-zinc-600">
                  {impact.signal?.signal_type
                    ? `${impact.signal.signal_type === "PROXY" ? "weather-estimated" : impact.signal.signal_type === "SYNTHETIC" ? "test" : impact.signal.signal_type === "FORECAST" ? "forecast" : impact.signal.signal_type.toLowerCase()} grid signal · ${impact.metrics.co2_basis === "FORECAST" ? "forecast-based estimate · " : ""}`
                    : ""}
                  {impact.metrics.deadline_misses === 0 ? "every deadline met" : `${impact.metrics.deadline_misses} deadline(s) missed`}
                </p>
              </div>
              <ul className="mt-4 divide-y divide-white/5">
                {impact.schedule.map((sj) => {
                  const ex = impact.explanations.find((e) => e.job_id === sj.job_id);
                  const wins = runWindows(sj.allocations);
                  return (
                    <li key={sj.job_id} className="py-2.5">
                      <p className="text-[13px] font-medium">{nameOf[sj.job_id] ?? sj.name}</p>
                      <p className="mt-0.5 font-mono text-[11px] text-zinc-500">
                        runs {wins.length > 0 ? wins.join(" · ") : "—"}
                        {ex && ex.co2_saved_kg !== null && ex.co2_saved_kg > 0.005 ? ` · saves ${ex.co2_saved_kg.toFixed(2)} kg` : ""}
                      </p>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          {liveState && (
            <div className="mt-4 border-t border-white/10 pt-4">
              <ExecutionPanel
                state={liveState}
                history={liveHistory}
                names={nameOf}
                busy={liveBusy}
                onReplan={() => void liveReplan()}
                onEvent={(jobId, type) => void liveEvent(jobId, type)}
                onOverride={(jobId, command, newDeadlineAt) => void liveOverride(jobId, command, newDeadlineAt)}
              />
            </div>
          )}
        </div>

        {/* Multi-user building coordination */}
        <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-4 sm:p-8">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-[15px] font-medium">Shared capacity</h2>
                <span className="rounded-full bg-lime-300/15 px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-lime-300">
                  {coordResult?.status ?? "Ready"}
                </span>
              </div>
              <p className="mt-0.5 font-mono text-[11px] text-zinc-600">
                All your loads scheduled together under one site limit, so they never overload it
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
                className="min-h-11 cursor-pointer rounded-full bg-white px-5 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
              >
                {coordBusy ? "Checking…" : "Re-check now"}
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
            </div>
          ) : (
            <div className="mt-4 border-t border-white/5 pt-4">
              <p className="mb-3 font-mono text-[11px] text-zinc-600">
                {jobs.length === 0
                  ? "Add a load above and its combined profile appears here."
                  : needsDetail.length === jobs.filter((j) => j.shiftable !== false).length && needsDetail.length > 0
                    ? "Fill in the missing details on your loads to see the combined profile."
                    : coordBusy
                      ? "Working out the combined profile…"
                      : "The combined profile appears here once the backend answers."}
              </p>
              <BuildingChart points={[]} />
            </div>
          )}
        </div>

        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <div className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Carbon signal</p>
            <h2 className="mt-2 text-[15px] font-medium">
              {signal
                ? signal.signal_type === "PROXY"
                  ? "live weather estimate"
                  : signal.signal_type === "SYNTHETIC"
                    ? "synthetic (test data)"
                    : signal.signal_type.toLowerCase()
                : "—"}
            </h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">
              {signal
                ? signal.signal_type === "PROXY"
                  ? "Built from real solar and wind forecasts for your area, not metered grid data. It rises and falls with the actual weather."
                  : signal.signal_type === "SYNTHETIC"
                    ? "A fixed test curve. Set CARBON_PROVIDER=weather on the backend for live data."
                    : `Source: ${signal.source}.`
                : "Waiting for the backend."}
            </p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Forecast</p>
            <h2 className="mt-2 text-[15px] font-medium">{forecast ? forecast.provenance.model : "unavailable"}</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">
              {forecast
                ? forecast.provenance.source_signal_type === "SYNTHETIC"
                  ? "Trained on placeholder history because live history was unavailable. Treat it as a rough guide only."
                  : `${Math.round(forecast.provenance.interval_nominal_coverage * 100)}% prediction band learned from the signal's own recent history. Use Expected or Robust in the chart to plan with it.`
                : "Planning still works against the live signal."}
            </p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Tracking</p>
            <h2 className="mt-2 text-[15px] font-medium">you confirm it</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">
              No smart meters or device control are connected, so you press Start and Done when an appliance actually runs. The planner uses the real clock.
            </p>
          </div>
        </div>
      </div>
    </main>
  );
}
