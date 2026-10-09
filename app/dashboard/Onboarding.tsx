"use client";

import { useEffect, useMemo, useState } from "react";
import { signOut, updateProfile, type User } from "firebase/auth";
import { collection, doc, getDoc, writeBatch } from "firebase/firestore";
import { getDb, getFirebaseAuth } from "../../lib/firebase";
import { claimUsername, validUsername } from "../../lib/username";
import { classifyJob, type JobInput } from "../../lib/prioritize";
import { detailNeeded, kindOf, type StoredJob } from "../../lib/loads/specs";
import { useJevClassify } from "../../lib/onboarding/useJevClassify";
import { useBackendRanking } from "../../lib/onboarding/useRanking";
import { OCCUPATIONS, PLACES, profileFor, saveMaxPower, type LoadPreset } from "../../lib/occupations";
import type { JobType } from "../../lib/api/types";

type Draft = {
  name: string;
  powerKw: string;
  readyBy: string;
  flexHours: number;
  energyKwh: string;
  durationMin: string;
  // Filled in from the backend classification of `name` (Jev or the built-in rules).
  jobType?: JobType;
  category?: string;
  confidence?: number;
};
type Availability = "free" | "mine" | "taken" | "unknown";

const BLANK: Draft = { name: "", powerKw: "", readyBy: "06:00", flexHours: 2, energyKwh: "", durationMin: "" };

const MAX_POWER_KW = 1000;

const inputCls =
  "w-full rounded-xl border border-white/10 bg-black px-4 py-2.5 text-sm text-white placeholder:text-zinc-700 focus:border-white/30 focus:outline-none";

const TYPE_NAME: Record<JobType, string> = {
  DEFERRABLE_INTERRUPTIBLE: "Can pause and resume",
  DEFERRABLE_ATOMIC: "Runs in one go",
  THERMAL: "Keeps a temperature",
  FIXED: "Always on",
};

const MAX_LOADS = 20;

function toStored(d: Draft, id: string): StoredJob {
  const c = classifyJob(d.name);
  const energy = Number(d.energyKwh);
  const dur = Number(d.durationMin);
  return {
    id,
    name: d.name.trim(),
    kind: d.category ?? c.category,
    shiftable: d.jobType ? d.jobType !== "FIXED" : c.shiftable,
    powerKw: Number(d.powerKw) || 0,
    readyBy: d.readyBy,
    flexHours: d.flexHours,
    jobType: d.jobType,
    energyKwh: energy > 0 ? energy : undefined,
    durationMin: dur > 0 ? Math.round(dur) : undefined,
    confidence: d.confidence,
  };
}

/** One load being described. Owns the live classification of its name. */
function DraftCard({
  d,
  i,
  onChange,
  onRemove,
}: {
  d: Draft;
  i: number;
  onChange: (patch: Partial<Draft>) => void;
  onRemove?: () => void;
}) {
  const cls = useJevClassify(d.name);
  const answer = cls.status === "ok" ? cls.answer : null;
  const ansType = answer?.jobType;
  const ansCategory = answer?.category;
  const ansConfidence = answer?.confidence;

  // Keep the draft's type in step with what the backend says about the current name.
  useEffect(() => {
    if (ansType && ansType !== d.jobType) {
      onChange({ jobType: ansType, category: ansCategory, confidence: ansConfidence });
    }
    // onChange identity changes every render of the parent; only the result matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ansType, ansCategory, ansConfidence, d.jobType]);

  const stored = toStored(d, `draft-${i}`);
  const hasName = d.name.trim().length > 0;
  const need = hasName ? detailNeeded(stored) : null;
  const kw = Number(d.powerKw);
  const powerBad = hasName && !(kw > 0 && kw <= MAX_POWER_KW);
  // What the plan will treat this load as. It matches the backend's answer except
  // where the name itself marks a heater or cooler as a temperature-holding load.
  const type: JobType = kindOf(stored);
  const pct = answer ? Math.round(answer.confidence * 100) : 0;

  return (
    <div className="rounded-2xl border border-white/10 p-4">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[11px] text-zinc-600">LOAD {i + 1}</span>
        {onRemove && (
          <button type="button" onClick={onRemove} className="-mr-2 min-h-10 cursor-pointer px-3 font-mono text-[12px] text-zinc-500 transition hover:text-red-300">
            remove
          </button>
        )}
      </div>
      <input
        aria-label={`Load ${i + 1} name`}
        value={d.name}
        onChange={(e) => onChange({ name: e.target.value, jobType: undefined, category: undefined, confidence: undefined })}
        placeholder="Name anything, e.g. hostel borewell pump"
        maxLength={40}
        className={`mt-3 ${inputCls}`}
      />
      {hasName && (
        <p aria-live="polite" className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px]">
          <span className={`h-1.5 w-1.5 rounded-full ${cls.status === "offline" ? "bg-orange-300" : type === "FIXED" ? "bg-zinc-600" : "bg-lime-300"}`} />
          {cls.status === "ok" && answer ? (
            <>
              <span className={answer.provider === "jev" ? "text-violet-300" : "text-zinc-400"}>
                {answer.provider === "jev"
                  ? `Jev: ${TYPE_NAME[answer.jobType]} · ${pct}% sure`
                  : `Built-in rules: ${TYPE_NAME[answer.jobType]}`}
              </span>
              <span className="text-zinc-600">{answer.category}</span>
              {type !== answer.jobType && <span className="text-zinc-500">· planned as &ldquo;{TYPE_NAME[type]}&rdquo;</span>}
              {answer.ambiguous && <span className="text-amber-300">· not certain, check the details below</span>}
            </>
          ) : cls.status === "offline" ? (
            <span className="text-orange-300">
              The classifier is not reachable, so this is a rough guess made in this page: {TYPE_NAME[type]}.
            </span>
          ) : (
            <span className="text-zinc-600">Checking what kind of load this is…</span>
          )}
          {type === "FIXED" && cls.status !== "idle" && (
            <span className="text-zinc-500">· counted in the plan, never moved</span>
          )}
        </p>
      )}
      <div className="mt-2 grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
        <label className={`rounded-xl border bg-black px-3 py-2 focus-within:border-white/30 ${powerBad ? "border-orange-300/50" : "border-white/10"}`}>
          <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">Power (kW)</span>
          <input
            value={d.powerKw}
            onChange={(e) => onChange({ powerKw: e.target.value.replace(/[^0-9.]/g, "") })}
            inputMode="decimal"
            placeholder="e.g. 2"
            className="w-full bg-transparent py-1 text-sm text-white placeholder:text-zinc-700 focus:outline-none"
          />
        </label>
        {type !== "FIXED" && (
          <label className="rounded-xl border border-white/10 bg-black px-3 py-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">Needed by</span>
            <input
              type="time"
              value={d.readyBy}
              onChange={(e) => onChange({ readyBy: e.target.value })}
              className="w-full cursor-pointer bg-transparent py-1 text-sm text-white focus:outline-none [color-scheme:dark]"
            />
          </label>
        )}
        {(type === "DEFERRABLE_INTERRUPTIBLE" || type === "DEFERRABLE_ATOMIC") && (
          <label className={`rounded-xl border bg-black px-3 py-2 focus-within:border-white/30 ${need ? "border-amber-400/50" : "border-white/10"}`}>
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">
              {type === "DEFERRABLE_INTERRUPTIBLE" ? "Energy needed (kWh)" : "One run takes (min)"}
            </span>
            <input
              value={type === "DEFERRABLE_INTERRUPTIBLE" ? d.energyKwh : d.durationMin}
              onChange={(e) =>
                onChange(
                  type === "DEFERRABLE_INTERRUPTIBLE"
                    ? { energyKwh: e.target.value.replace(/[^0-9.]/g, "") }
                    : { durationMin: e.target.value.replace(/[^0-9]/g, "") }
                )
              }
              inputMode="decimal"
              placeholder={type === "DEFERRABLE_INTERRUPTIBLE" ? "e.g. 20" : "e.g. 60"}
              className="w-full bg-transparent py-1 text-sm text-white placeholder:text-zinc-700 focus:outline-none"
            />
          </label>
        )}
        {type !== "FIXED" && (
          <label className="rounded-xl border border-white/10 bg-black px-3 py-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">Can finish up to {d.flexHours} h late</span>
            <input
              type="range"
              min={0}
              max={6}
              value={d.flexHours}
              onChange={(e) => onChange({ flexHours: Number(e.target.value) })}
              className="mt-1 h-6 w-full cursor-pointer accent-lime-300"
            />
          </label>
        )}
      </div>
      {type === "THERMAL" && (
        <p className="mt-2 font-mono text-[11px] text-zinc-600">
          Keeps a temperature: the plan uses a typical comfort band and heat-loss model (estimates, not measurements).
        </p>
      )}
      {(powerBad || need) && (
        <p className="mt-2 font-mono text-[11px] text-orange-300">
          {powerBad
            ? kw > MAX_POWER_KW
              ? `Keep it at or under ${MAX_POWER_KW} kW.`
              : "Add the power rating in kW (check the appliance label)."
            : type === "DEFERRABLE_INTERRUPTIBLE"
              ? "Total energy in kWh (roughly its kW rating × hours it runs)."
              : "How long does one run take, in minutes?"}
        </p>
      )}
    </div>
  );
}

export default function Onboarding({ user, onDone }: { user: User; onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState(user.displayName ?? "");
  const [username, setUsername] = useState("");
  const [occupation, setOccupation] = useState("");
  const [place, setPlace] = useState("");
  const [rooms, setRooms] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([{ ...BLANK }]);
  const [err, setErr] = useState("");
  const [working, setWorking] = useState(false);
  // Availability is only trusted for the exact name it was checked for, so a
  // stale answer is ignored instead of cleared (no setState inside the effect body).
  const [avail, setAvail] = useState<{ name: string; state: Availability } | null>(null);

  const uname = username.trim().toLowerCase();
  const unameValid = validUsername(uname);
  const availability: Availability | "checking" | "idle" = !unameValid
    ? "idle"
    : avail && avail.name === uname
      ? avail.state
      : "checking";

  // Check the name as they type, so a taken name is caught on step 1 instead of
  // after four steps of typing. The registry is publicly readable by design.
  useEffect(() => {
    if (!unameValid) return;
    const db = getDb();
    if (!db) return;
    let cancelled = false;
    const t = setTimeout(() => {
      getDoc(doc(db, "usernames", uname))
        .then((snap) => {
          if (cancelled) return;
          const state: Availability = !snap.exists() ? "free" : snap.data().uid === user.uid ? "mine" : "taken";
          setAvail({ name: uname, state });
        })
        .catch(() => {
          // Offline / rules: don't block; the final save re-checks atomically.
          if (!cancelled) setAvail({ name: uname, state: "unknown" });
        });
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [uname, unameValid, user.uid]);

  const stored: StoredJob[] = useMemo(
    () => drafts.filter((d) => d.name.trim() && d.readyBy).map((d, i) => toStored(d, `draft-${i}`)),
    [drafts]
  );
  const jobs: JobInput[] = stored;
  // Step 4 asks the backend (Jev when it is available) which load matters first.
  const ranking = useBackendRanking(jobs, step === 3);
  const rankedNow = ranking.status === "loading" ? [] : ranking.ranked;
  const mixedSources = rankedNow.some((j) => j.source === "jev") && rankedNow.some((j) => j.source !== "jev");
  const loadsValid =
    stored.length > 0 && stored.every((j) => j.powerKw > 0 && j.powerKw <= MAX_POWER_KW && detailNeeded(j) === null);

  // What the person told us about their building decides the presets, the starting
  // "Max power at once" and the advice line.
  const roomCount = rooms ? Number(rooms) : null;
  const profile = useMemo(() => profileFor(occupation, place, roomCount), [occupation, place, roomCount]);
  const [showMore, setShowMore] = useState(false);
  const [maxPowerEdit, setMaxPowerEdit] = useState<string | null>(null);
  const maxPowerText = maxPowerEdit ?? String(profile.capacityKw);
  const maxPowerKw = Number(maxPowerText);
  const biggestLoadKw = stored.reduce((m, j) => Math.max(m, j.powerKw), 0);
  const maxPowerError =
    !(maxPowerKw > 0 && maxPowerKw <= MAX_POWER_KW)
      ? `Enter a number above 0 and up to ${MAX_POWER_KW} kW.`
      : biggestLoadKw > maxPowerKw
        ? `Your biggest load needs ${biggestLoadKw} kW, more than ${maxPowerKw} kW. Raise this limit or lower that load's power.`
        : null;

  function patchDraft(i: number, patch: Partial<Draft>) {
    setDrafts((ds) => ds.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  }

  function addPreset(p: LoadPreset) {
    const next: Draft = {
      name: p.name,
      powerKw: String(p.powerKw),
      readyBy: p.readyBy,
      flexHours: p.flexHours,
      energyKwh: p.energyKwh !== undefined ? String(p.energyKwh) : "",
      durationMin: p.durationMin !== undefined ? String(p.durationMin) : "",
    };
    setDrafts((ds) => {
      if (ds.length >= MAX_LOADS && !ds.some((d) => !d.name.trim() && !d.powerKw)) return ds;
      // Reuse the untouched blank row instead of leaving an empty card behind.
      const blank = ds.findIndex((d) => !d.name.trim() && !d.powerKw);
      if (blank >= 0) return ds.map((d, j) => (j === blank ? next : d));
      return [...ds, next];
    });
  }

  function canNext() {
    if (step === 0) return name.trim().length > 0 && unameValid && availability !== "taken" && availability !== "checking";
    if (step === 1) return occupation !== "" && place !== "";
    if (step === 2) return loadsValid;
    return maxPowerError === null;
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (working) return;
    if (step < 3) {
      if (canNext()) {
        setErr("");
        setStep(step + 1);
      }
    } else {
      void finish();
    }
  }

  async function finish() {
    setErr("");
    // Step gating can be bypassed via Back/Continue edits — re-validate
    // everything before writing.
    if (!name.trim()) {
      setErr("Enter a display name first.");
      return;
    }
    if (!unameValid) {
      setErr("Pick a valid username first (3–20 chars: lowercase, numbers, underscore).");
      return;
    }
    if (!occupation || !place) {
      setErr("Pick an occupation and a place first.");
      return;
    }
    if (!loadsValid) {
      setErr("Add at least one load, each with its power rating and the detail it asks for.");
      return;
    }
    if (maxPowerError) {
      setErr(maxPowerError);
      return;
    }
    const db = getDb();
    if (!db) {
      setErr("Database not configured.");
      return;
    }
    setWorking(true);
    try {
      const vName = name.trim();
      if (vName !== user.displayName) await updateProfile(user, { displayName: vName });
      let prev: string | null = null;
      try {
        const existing = await getDoc(doc(db, "users", user.uid));
        if (existing.exists()) prev = (existing.data().username as string | undefined) ?? null;
      } catch {
        /* profile unreadable — claim without releasing a prev name */
      }
      // 1) Claim the name (atomic; the only step that can fail with "taken").
      //    Re-running it on a retry is idempotent for the same user.
      // The email is deliberately NOT stored: this document is world-readable.
      await claimUsername(db, user.uid, uname, prev, {
        displayName: vName,
        photoURL: user.photoURL ?? null,
      });
      // 2) Loads and the `onboarded` flag land in ONE atomic batch. If it fails,
      //    nothing is half-written, so a retry cannot duplicate loads or leave
      //    the user "onboarded" with an empty list.
      const batch = writeBatch(db);
      const jobsCol = collection(db, "users", user.uid, "jobs");
      for (const j of stored) {
        batch.set(doc(jobsCol), {
          name: j.name,
          kind: j.kind,
          shiftable: j.shiftable ?? true,
          powerKw: j.powerKw,
          readyBy: j.readyBy,
          flexHours: j.flexHours,
          // Optional fields are written only when known, so a missing value stays missing.
          ...(j.jobType ? { jobType: j.jobType } : {}),
          ...(j.energyKwh ? { energyKwh: j.energyKwh } : {}),
          ...(j.durationMin ? { durationMin: j.durationMin } : {}),
          ...(j.confidence !== undefined ? { confidence: j.confidence } : {}),
          createdAt: new Date().toISOString(),
        });
      }
      batch.set(
        doc(db, "users", user.uid),
        { occupation, place, rooms: rooms ? Number(rooms) : null, onboarded: true },
        { merge: true }
      );
      await batch.commit();
      saveMaxPower(user.uid, maxPowerKw);
      onDone();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "";
      const code = e instanceof Error && "code" in e ? (e as { code?: string }).code : undefined;
      if (msg === "taken") {
        setAvail({ name: uname, state: "taken" });
        setStep(0);
        setErr(`@${uname} was just taken. Pick another.`);
      } else if (code === "permission-denied") setErr("Saving was blocked by the database security rules. If this is your own project, deploy firestore.rules.");
      else if (code === "not-found" || /does not exist/i.test(msg)) setErr("The Firestore database doesn't exist yet — create it in the Firebase console, then retry.");
      else if (code === "unavailable") setErr("You appear to be offline. Check your connection and retry.");
      else setErr(`Couldn't save (${code ?? "unknown"}). Try again.`);
      setWorking(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-start overflow-y-auto bg-black/80 p-3 backdrop-blur-sm sm:place-items-center sm:p-4">
      <form
        onSubmit={onSubmit}
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        className="my-auto w-full max-w-xl rounded-3xl border border-white/10 bg-[#0a0a0a] p-5 sm:p-9"
      >
        <div className="flex items-center gap-2" aria-hidden="true">
          {[0, 1, 2, 3].map((i) => (
            <span key={i} className={`h-1 flex-1 rounded-full transition-colors ${i <= step ? "bg-lime-300" : "bg-white/10"}`} />
          ))}
        </div>
        <p className="mt-3 font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Step {step + 1} of 4</p>

        {step === 0 && (
          <div>
            <h2 id="onboarding-title" className="mt-3 text-2xl font-semibold tracking-tight">What should we call you?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">Your name and a username for your public page.</p>
            <label htmlFor="ob-name" className="mt-6 block text-[13px] text-zinc-400">Display name</label>
            <input id="ob-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={40} placeholder="Your name" className={`mt-2 ${inputCls}`} />
            <label htmlFor="ob-username" className="mt-5 block text-[13px] text-zinc-400">Username</label>
            <div className="mt-2 flex items-center rounded-xl border border-white/10 bg-black px-4 focus-within:border-white/30">
              <span className="font-mono text-sm text-zinc-600">@</span>
              <input
                id="ob-username"
                value={username}
                onChange={(e) => setUsername(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))}
                maxLength={20}
                placeholder="heliophile"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                className="w-full bg-transparent px-1 py-2.5 font-mono text-sm text-white placeholder:text-zinc-700 focus:outline-none"
              />
            </div>
            <p aria-live="polite" className="mt-2 min-h-4 font-mono text-[11px]">
              {!username ? (
                <span className="text-zinc-600">3–20 letters, numbers or _ · your page is /username</span>
              ) : !unameValid ? (
                <span className="text-orange-300">
                  {uname.length < 3 ? "At least 3 characters." : "That name is reserved — pick another."}
                </span>
              ) : availability === "checking" ? (
                <span className="text-zinc-500">checking @{uname}…</span>
              ) : availability === "taken" ? (
                <span className="text-orange-300">@{uname} is taken. Try another.</span>
              ) : availability === "unknown" ? (
                <span className="text-zinc-500">Couldn&apos;t check @{uname} right now; it will be verified when you save.</span>
              ) : (
                <span className="text-lime-300">@{uname} is available · your public page will be /{uname}</span>
              )}
            </p>
          </div>
        )}

        {step === 1 && (
          <div>
            <h2 id="onboarding-title" className="mt-3 text-2xl font-semibold tracking-tight">What do you do?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">
              This picks your starter appliances and a suggested power limit.
            </p>
            <p className="mt-6 text-[13px] text-zinc-400">Occupation</p>
            <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Occupation">
              {OCCUPATIONS.map((o) => (
                <button
                  key={o}
                  type="button"
                  role="radio"
                  aria-checked={occupation === o}
                  onClick={() => setOccupation(o)}
                  className={`min-h-11 cursor-pointer rounded-full border px-4 py-2 text-[13px] transition active:scale-[0.96] ${
                    occupation === o ? "border-lime-300 bg-lime-300 text-black font-medium" : "border-white/15 text-zinc-400 hover:border-white/40 hover:text-white"
                  }`}
                >
                  {o}
                </button>
              ))}
            </div>
            <p className="mt-6 text-[13px] text-zinc-400">Where will this run?</p>
            <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Place">
              {PLACES.map((p) => (
                <button
                  key={p}
                  type="button"
                  role="radio"
                  aria-checked={place === p}
                  onClick={() => setPlace(p)}
                  className={`min-h-11 cursor-pointer rounded-full border px-4 py-2 text-[13px] transition active:scale-[0.96] ${
                    place === p ? "border-lime-300 bg-lime-300 text-black font-medium" : "border-white/15 text-zinc-400 hover:border-white/40 hover:text-white"
                  }`}
                >
                  {p}
                </button>
              ))}
            </div>
            <label htmlFor="ob-rooms" className="mt-6 block text-[13px] text-zinc-400">Rooms (optional)</label>
            <input id="ob-rooms" value={rooms} onChange={(e) => setRooms(e.target.value.replace(/[^0-9]/g, "").slice(0, 4))} inputMode="numeric" placeholder="e.g. 40" className={`mt-2 ${inputCls}`} />
            {occupation && place ? (
              <div className="mt-6 rounded-xl border border-lime-300/20 bg-lime-300/[0.03] px-4 py-3">
                <p className="text-[13px] leading-6 text-zinc-300">{profile.advice}</p>
                <p title={profile.capacityNote} className="mt-2 font-mono text-[11px] text-zinc-500">
                  Suggested max power at once: {profile.capacityKw} kW
                </p>
              </div>
            ) : null}
            <p className="mt-5 font-mono text-[11px] leading-5 text-zinc-600">
              Occupation, place and rooms are public on your profile; your loads stay private.
            </p>
          </div>
        )}

        {step === 2 && (
          <div>
            <h2 id="onboarding-title" className="mt-3 text-2xl font-semibold tracking-tight">Which loads should we optimize?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">
              Add what you have; we ask only for the numbers each kind needs.
            </p>
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11px] text-zinc-600">quick add</span>
              {(showMore ? [...profile.primary, ...profile.more] : profile.primary).map((p) => {
                const added = drafts.some((d) => d.name.trim().toLowerCase() === p.name.toLowerCase());
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => addPreset(p)}
                    disabled={added}
                    className="min-h-10 cursor-pointer rounded-full border border-white/15 px-3.5 py-2 text-[12px] text-zinc-300 transition hover:border-lime-300/60 hover:text-white active:scale-[0.96] disabled:cursor-default disabled:border-white/5 disabled:text-zinc-600 disabled:hover:border-white/5"
                  >
                    {added ? "✓" : "+"} {p.label}
                  </button>
                );
              })}
              <button
                type="button"
                onClick={() => setShowMore((v) => !v)}
                aria-expanded={showMore}
                className="min-h-10 cursor-pointer px-2 text-[12px] text-zinc-500 underline decoration-white/20 underline-offset-4 transition hover:text-white"
              >
                {showMore ? "Fewer loads" : "More loads"}
              </button>
            </div>
            <p className="mt-2 font-mono text-[11px] leading-5 text-zinc-600">
              Presets use typical numbers; check them against your appliance label.
            </p>
            <div className="mt-4 space-y-3">
              {drafts.map((d, i) => (
                <DraftCard
                  key={i}
                  d={d}
                  i={i}
                  onChange={(patch) => patchDraft(i, patch)}
                  onRemove={drafts.length > 1 ? () => setDrafts((ds) => ds.filter((_, j) => j !== i)) : undefined}
                />
              ))}
            </div>
            <button
              type="button"
              disabled={drafts.length >= MAX_LOADS}
              onClick={() => setDrafts((ds) => [...ds, { ...BLANK, readyBy: "18:00" }])}
              className="mt-3 min-h-12 w-full cursor-pointer rounded-xl border border-dashed border-white/15 py-3 text-sm text-zinc-400 transition hover:border-white/40 hover:text-white active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40"
            >
              + Add another load
            </button>
          </div>
        )}

        {step === 3 && (
          <div>
            <h2 id="onboarding-title" className="mt-3 text-2xl font-semibold tracking-tight">Review and save</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">
              @{uname} · {occupation} · {place}
              {rooms ? ` · ${rooms} rooms` : ""}
            </p>
            <label htmlFor="ob-maxpower" className="mt-5 block text-[13px] text-zinc-400">Max power at once (kW)</label>
            <input
              id="ob-maxpower"
              value={maxPowerText}
              onChange={(e) => setMaxPowerEdit(e.target.value.replace(/[^0-9.]/g, ""))}
              inputMode="decimal"
              aria-invalid={maxPowerError !== null}
              className={`mt-2 ${inputCls} ${maxPowerError ? "border-orange-300/50" : ""}`}
            />
            <p
              title={maxPowerEdit === null ? profile.capacityNote : undefined}
              className={`mt-2 font-mono text-[11px] leading-5 ${maxPowerError ? "text-orange-300" : "text-zinc-600"}`}
            >
              {maxPowerError ?? "The most all your loads may draw together. The dashboard plans within it."}
            </p>
            {ranking.status === "loading" ? (
              <p className="mt-5 flex items-center gap-2 font-mono text-[11px] text-zinc-500">
                <span className="spinner" /> Asking the backend which load matters first…
              </p>
            ) : ranking.ranked.length > 0 ? (
              <>
                <p className="mt-5 font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Priority order</p>
                <p className={`mt-1 font-mono text-[11px] leading-5 ${ranking.status === "fallback" ? "text-orange-300" : "text-zinc-500"}`}>{ranking.label}</p>
                <ol className="mt-2 space-y-2">
                  {ranking.ranked.map((j, i) => (
                    <li key={j.id} title={j.reason} className="flex items-center gap-3 rounded-xl border border-white/10 px-3.5 py-3 sm:gap-4 sm:px-4">
                      <span className="font-mono text-[12px] text-zinc-600">{String(i + 1).padStart(2, "0")}</span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium">{j.name}</p>
                      </div>
                      {mixedSources && j.source === "jev" && (
                        <span className="shrink-0 rounded-full bg-violet-400/15 px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider text-violet-300">jev</span>
                      )}
                      <span className={`shrink-0 rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider ${j.band === "Critical" ? "bg-red-500/15 text-red-300" : j.band === "High" ? "bg-orange-400/15 text-orange-300" : j.band === "Normal" ? "bg-white/10 text-zinc-300" : "bg-white/5 text-zinc-500"}`}>
                        {j.band}
                      </span>
                    </li>
                  ))}
                </ol>
              </>
            ) : (
              <p className="mt-5 rounded-xl border border-white/10 px-4 py-3 text-sm leading-6 text-zinc-400">
                Everything here is always on (like a fridge), so nothing can shift yet. Go back and add a flexible load such as EV charging.
              </p>
            )}
            <p className="mt-4 font-mono text-[11px] text-zinc-600">
              {stored.length} load{stored.length === 1 ? "" : "s"} will be saved to your account.
            </p>
          </div>
        )}

        {err && <p role="alert" className="mt-4 font-mono text-[12px] text-orange-300">{err}</p>}

        <div className="mt-7 flex items-center justify-between gap-3">
          {step > 0 ? (
            <button type="button" onClick={() => { setErr(""); setStep(step - 1); }} disabled={working} className="min-h-11 cursor-pointer px-2 text-sm text-zinc-500 transition hover:text-white disabled:opacity-50">
              ← Back
            </button>
          ) : (
            <button
              type="button"
              onClick={() => {
                const a = getFirebaseAuth();
                if (a) void signOut(a);
              }}
              className="min-h-11 cursor-pointer px-1 font-mono text-[11px] text-zinc-600 transition hover:text-white"
            >
              Not you? Sign out
            </button>
          )}
          {step < 3 ? (
            <button
              type="submit"
              disabled={!canNext()}
              className="min-h-11 cursor-pointer rounded-full bg-white px-8 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30"
            >
              Continue
            </button>
          ) : (
            <button
              type="submit"
              disabled={working || !loadsValid || maxPowerError !== null}
              className="flex min-h-11 cursor-pointer items-center gap-2 rounded-full bg-lime-300 px-8 py-2.5 text-sm font-medium text-black transition hover:bg-lime-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
            >
              {working && <span className="spinner" />}
              {working ? "Saving…" : "Save & finish"}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
