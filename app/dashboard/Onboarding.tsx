"use client";

import { useEffect, useState } from "react";
import { signOut, updateProfile, type User } from "firebase/auth";
import { collection, doc, getDoc, writeBatch } from "firebase/firestore";
import { getDb, getFirebaseAuth } from "../../lib/firebase";
import { claimUsername, validUsername } from "../../lib/username";
import { jevRank, classifyJob, type JobInput } from "../../lib/prioritize";

type Draft = { name: string; powerKw: string; readyBy: string; flexHours: number };
type Availability = "free" | "mine" | "taken" | "unknown";

const OCCUPATIONS = ["Student", "Hostel staff", "Homeowner", "Facility manager", "Researcher", "Other"];
const PLACES = ["Hostel block", "Home", "Campus", "Other"];

// One-click starting points so a first load takes seconds, not a form.
const PRESETS: Array<Omit<Draft, "flexHours"> & { flexHours: number; label: string }> = [
  { label: "EV charger", name: "EV charger", powerKw: "7.4", readyBy: "07:00", flexHours: 3 },
  { label: "Water heater", name: "Water heater", powerKw: "2", readyBy: "06:00", flexHours: 2 },
  { label: "Washing machine", name: "Washing machine", powerKw: "2", readyBy: "18:00", flexHours: 4 },
  { label: "Borewell pump", name: "Borewell pump", powerKw: "1.5", readyBy: "08:00", flexHours: 3 },
];

const MAX_POWER_KW = 1000;

const inputCls =
  "w-full rounded-xl border border-white/10 bg-black px-4 py-2.5 text-sm text-white placeholder:text-zinc-700 focus:border-white/30 focus:outline-none";

export default function Onboarding({ user, onDone }: { user: User; onDone: () => void }) {
  const [step, setStep] = useState(0);
  const [name, setName] = useState(user.displayName ?? "");
  const [username, setUsername] = useState("");
  const [occupation, setOccupation] = useState("");
  const [place, setPlace] = useState("");
  const [rooms, setRooms] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([{ name: "", powerKw: "", readyBy: "06:00", flexHours: 2 }]);
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

  const jobs: JobInput[] = drafts
    .filter((d) => d.name.trim() && d.readyBy)
    .map((d, i) => {
      const c = classifyJob(d.name);
      return {
        id: `draft-${i}`,
        name: d.name.trim(),
        kind: c.category,
        shiftable: c.shiftable,
        powerKw: Number(d.powerKw) || 0,
        readyBy: d.readyBy,
        flexHours: d.flexHours,
      };
    });
  const ranked = jevRank(jobs);
  const powerOk = (kw: number) => kw > 0 && kw <= MAX_POWER_KW;
  const loadsValid = jobs.length > 0 && jobs.every((j) => powerOk(j.powerKw));

  function setDraft(i: number, patch: Partial<Draft>) {
    setDrafts((ds) => ds.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  }

  function addPreset(p: (typeof PRESETS)[number]) {
    const next: Draft = { name: p.name, powerKw: p.powerKw, readyBy: p.readyBy, flexHours: p.flexHours };
    setDrafts((ds) => {
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
    return true;
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
      setErr("Add at least one load, each with a power rating above 0 kW.");
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
      for (const j of jobs) {
        batch.set(doc(jobsCol), {
          name: j.name,
          kind: j.kind,
          shiftable: j.shiftable ?? true,
          powerKw: j.powerKw,
          readyBy: j.readyBy,
          flexHours: j.flexHours,
          createdAt: new Date().toISOString(),
        });
      }
      batch.set(
        doc(db, "users", user.uid),
        { occupation, place, rooms: rooms ? Number(rooms) : null, onboarded: true },
        { merge: true }
      );
      await batch.commit();
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
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-black/80 p-4 backdrop-blur-sm">
      <form
        onSubmit={onSubmit}
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        className="w-full max-w-xl rounded-3xl border border-white/10 bg-[#0a0a0a] p-7 sm:p-9"
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
            <p className="mt-2 text-sm leading-6 text-zinc-500">Your name, and a unique username for your public page.</p>
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
                <span className="text-zinc-600">3–20 chars · lowercase, numbers, underscore · your public page is /username</span>
              ) : !unameValid ? (
                <span className="text-orange-300">
                  {uname.length < 3 ? "At least 3 characters." : "That name is reserved — pick another."}
                </span>
              ) : availability === "checking" ? (
                <span className="text-zinc-500">checking @{uname}…</span>
              ) : availability === "taken" ? (
                <span className="text-orange-300">@{uname} is taken. Try another.</span>
              ) : (
                <span className="text-lime-300">@{uname} is available · your public page will be /{uname}</span>
              )}
            </p>
          </div>
        )}

        {step === 1 && (
          <div>
            <h2 id="onboarding-title" className="mt-3 text-2xl font-semibold tracking-tight">What do you do?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">So we size the advice to your building, not a generic home.</p>
            <p className="mt-6 text-[13px] text-zinc-400">Occupation</p>
            <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Occupation">
              {OCCUPATIONS.map((o) => (
                <button
                  key={o}
                  type="button"
                  role="radio"
                  aria-checked={occupation === o}
                  onClick={() => setOccupation(o)}
                  className={`cursor-pointer rounded-full border px-4 py-2 text-[13px] transition active:scale-[0.96] ${
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
                  className={`cursor-pointer rounded-full border px-4 py-2 text-[13px] transition active:scale-[0.96] ${
                    place === p ? "border-lime-300 bg-lime-300 text-black font-medium" : "border-white/15 text-zinc-400 hover:border-white/40 hover:text-white"
                  }`}
                >
                  {p}
                </button>
              ))}
            </div>
            <label htmlFor="ob-rooms" className="mt-6 block text-[13px] text-zinc-400">Rooms (optional)</label>
            <input id="ob-rooms" value={rooms} onChange={(e) => setRooms(e.target.value.replace(/[^0-9]/g, "").slice(0, 4))} inputMode="numeric" placeholder="e.g. 40" className={`mt-2 ${inputCls}`} />
          </div>
        )}

        {step === 2 && (
          <div>
            <h2 id="onboarding-title" className="mt-3 text-2xl font-semibold tracking-tight">Which loads should we optimize?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">Add what you have. You can add more any time from the dashboard.</p>
            <div className="mt-5 flex flex-wrap items-center gap-2">
              <span className="font-mono text-[11px] text-zinc-600">quick add</span>
              {PRESETS.map((p) => (
                <button
                  key={p.label}
                  type="button"
                  onClick={() => addPreset(p)}
                  className="cursor-pointer rounded-full border border-white/15 px-3 py-1.5 text-[12px] text-zinc-300 transition hover:border-lime-300/60 hover:text-white active:scale-[0.96]"
                >
                  + {p.label}
                </button>
              ))}
            </div>
            <div className="mt-4 space-y-3">
              {drafts.map((d, i) => {
                const kw = Number(d.powerKw);
                const showPowerHint = d.name.trim() && !powerOk(kw);
                return (
                  <div key={i} className="rounded-2xl border border-white/10 p-4">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-[11px] text-zinc-600">LOAD {i + 1}</span>
                      {drafts.length > 1 && (
                        <button type="button" onClick={() => setDrafts((ds) => ds.filter((_, j) => j !== i))} className="cursor-pointer font-mono text-[11px] text-zinc-600 transition hover:text-red-300">
                          remove
                        </button>
                      )}
                    </div>
                    <input aria-label={`Load ${i + 1} name`} value={d.name} onChange={(e) => setDraft(i, { name: e.target.value })} placeholder="Name anything — e.g. hostel borewell pump" maxLength={40} className={`mt-3 ${inputCls}`} />
                    {(() => {
                      const c = classifyJob(d.name.trim() || "…");
                      return d.name.trim() ? (
                        <p className="mt-2 flex items-center gap-2 font-mono text-[11px]">
                          <span className={`h-1.5 w-1.5 rounded-full ${c.shiftable ? "bg-lime-300" : "bg-zinc-600"}`} />
                          <span className={c.shiftable ? "text-zinc-400" : "text-zinc-500"}>
                            {c.category} · {c.shiftable ? c.why : `${c.why} — filtered out of Optimize`}
                          </span>
                        </p>
                      ) : null;
                    })()}
                    <div className="mt-2 grid grid-cols-3 gap-2">
                      <label className={`rounded-xl border bg-black px-3 py-2 focus-within:border-white/30 ${showPowerHint ? "border-orange-300/50" : "border-white/10"}`}>
                        <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">power (kW)</span>
                        <input value={d.powerKw} onChange={(e) => setDraft(i, { powerKw: e.target.value.replace(/[^0-9.]/g, "") })} inputMode="decimal" placeholder="e.g. 2" className="w-full bg-transparent text-sm text-white placeholder:text-zinc-700 focus:outline-none" />
                      </label>
                      <label className="rounded-xl border border-white/10 bg-black px-3 py-2">
                        <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">ready by</span>
                        <input type="time" value={d.readyBy} onChange={(e) => setDraft(i, { readyBy: e.target.value })} className="w-full cursor-pointer bg-transparent text-sm text-white focus:outline-none [color-scheme:dark]" />
                      </label>
                      <label className="rounded-xl border border-white/10 bg-black px-3 py-2">
                        <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">flexible +{d.flexHours}h</span>
                        <input type="range" min={0} max={6} value={d.flexHours} onChange={(e) => setDraft(i, { flexHours: Number(e.target.value) })} className="w-full cursor-pointer accent-lime-300" />
                      </label>
                    </div>
                    {showPowerHint && (
                      <p className="mt-2 font-mono text-[11px] text-orange-300">
                        {kw > MAX_POWER_KW ? `Keep it at or under ${MAX_POWER_KW} kW.` : "Add the power rating in kW (check the appliance label)."}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
            <button
              type="button"
              onClick={() => setDrafts((ds) => [...ds, { name: "", powerKw: "", readyBy: "18:00", flexHours: 2 }])}
              className="mt-3 w-full cursor-pointer rounded-xl border border-dashed border-white/15 py-3 text-sm text-zinc-400 transition hover:border-white/40 hover:text-white active:scale-[0.99]"
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
              {rooms ? ` · ${rooms} rooms` : ""}. Next, the dashboard plans these into the cleanest hours of the grid.
            </p>
            {ranked.length > 0 ? (
              <>
                <p className="mt-5 font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Priority order</p>
                <ol className="mt-2 space-y-2">
                  {ranked.map((j, i) => (
                    <li key={j.id} className="flex items-center gap-4 rounded-xl border border-white/10 px-4 py-3">
                      <span className="font-mono text-[12px] text-zinc-600">{String(i + 1).padStart(2, "0")}</span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium">{j.name}</p>
                        <p className="mt-0.5 truncate font-mono text-[11px] text-zinc-500">{j.reason}</p>
                      </div>
                      <span className={`shrink-0 rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider ${j.band === "Critical" ? "bg-red-500/15 text-red-300" : j.band === "High" ? "bg-orange-400/15 text-orange-300" : j.band === "Normal" ? "bg-white/10 text-zinc-300" : "bg-white/5 text-zinc-500"}`}>
                        {j.band}
                      </span>
                    </li>
                  ))}
                </ol>
              </>
            ) : (
              <p className="mt-5 rounded-xl border border-white/10 px-4 py-3 text-sm leading-6 text-zinc-400">
                Everything you added is always-on (like a fridge), so there is nothing to shift yet. Go back and add a
                flexible load such as EV charging or water heating to see savings.
              </p>
            )}
            <p className="mt-4 font-mono text-[11px] text-zinc-600">
              {jobs.length} load{jobs.length === 1 ? "" : "s"} will be saved to your account.
            </p>
          </div>
        )}

        {err && <p role="alert" className="mt-4 font-mono text-[12px] text-orange-300">{err}</p>}

        <div className="mt-7 flex items-center justify-between">
          {step > 0 ? (
            <button type="button" onClick={() => { setErr(""); setStep(step - 1); }} disabled={working} className="cursor-pointer text-sm text-zinc-500 transition hover:text-white disabled:opacity-50">
              ← Back
            </button>
          ) : (
            <button
              type="button"
              onClick={() => {
                const a = getFirebaseAuth();
                if (a) void signOut(a);
              }}
              className="cursor-pointer font-mono text-[11px] text-zinc-600 transition hover:text-white"
            >
              Not you? Sign out
            </button>
          )}
          {step < 3 ? (
            <button
              type="submit"
              disabled={!canNext()}
              className="cursor-pointer rounded-full bg-white px-7 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30"
            >
              Continue
            </button>
          ) : (
            <button
              type="submit"
              disabled={working || !loadsValid}
              className="flex cursor-pointer items-center gap-2 rounded-full bg-lime-300 px-8 py-2.5 text-sm font-medium text-black transition hover:bg-lime-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
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
