"use client";

import { useState } from "react";
import { updateProfile, type User } from "firebase/auth";
import { addDoc, collection, doc, setDoc } from "firebase/firestore";
import { getDb } from "../../lib/firebase";
import { claimUsername, validUsername } from "../../lib/username";
import { jevRank, classifyJob, type JobInput } from "../../lib/prioritize";

type Draft = { name: string; powerKw: string; readyBy: string; flexHours: number };

const OCCUPATIONS = ["Student", "Hostel staff", "Homeowner", "Facility manager", "Researcher", "Other"];
const PLACES = ["Hostel block", "Home", "Campus", "Other"];

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

  function setDraft(i: number, patch: Partial<Draft>) {
    setDrafts((ds) => ds.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  }

  function canNext() {
    if (step === 0) return name.trim().length > 0 && validUsername(username.trim().toLowerCase());
    if (step === 1) return occupation !== "" && place !== "";
    if (step === 2) return jobs.length > 0;
    return true;
  }

  async function finish() {
    setErr("");
    const db = getDb();
    if (!db) {
      setErr("Database not configured.");
      return;
    }
    setWorking(true);
    try {
      const vName = name.trim();
      if (vName !== user.displayName) await updateProfile(user, { displayName: vName });
      const claimed = await claimUsername(db, user.uid, username, null, {
        displayName: vName,
        email: user.email ?? null,
        photoURL: user.photoURL ?? null,
      });
      await setDoc(
        doc(db, "users", user.uid),
        { occupation, place, rooms: rooms ? Number(rooms) : null, onboarded: true },
        { merge: true }
      );
      for (const j of jobs) {
        await addDoc(collection(db, "users", user.uid, "jobs"), {
          name: j.name,
          kind: j.kind,
          shiftable: j.shiftable ?? true,
          powerKw: j.powerKw,
          readyBy: j.readyBy,
          flexHours: j.flexHours,
          createdAt: new Date().toISOString(),
        });
      }
      void claimed;
      onDone();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "";
      const code = e instanceof Error && "code" in e ? (e as { code?: string }).code : undefined;
      if (msg === "taken") setErr(`@${username.trim().toLowerCase()} is taken. Try another.`);
      else if (code === "permission-denied") setErr("Firestore rules are blocking this — apply the project rules, then retry.");
      else if (code === "not-found" || /does not exist/i.test(msg)) setErr("Firestore database doesn't exist yet — create it, then retry.");
      else setErr(`Couldn't save (${code ?? "unknown"}). Try again.`);
      setWorking(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-black/80 p-4 backdrop-blur-sm">
      <div className="w-full max-w-xl rounded-3xl border border-white/10 bg-[#0a0a0a] p-7 sm:p-9">
        <div className="flex items-center gap-2">
          {[0, 1, 2, 3].map((i) => (
            <span key={i} className={`h-1 flex-1 rounded-full transition-colors ${i <= step ? "bg-lime-300" : "bg-white/10"}`} />
          ))}
        </div>

        {step === 0 && (
          <div>
            <h2 className="mt-6 text-2xl font-semibold tracking-tight">What should we call you?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">Your name, and a unique username for your public page.</p>
            <label className="mt-6 block text-[13px] text-zinc-400">Display name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={40} placeholder="Your name" className={`mt-2 ${inputCls}`} />
            <label className="mt-5 block text-[13px] text-zinc-400">Username</label>
            <div className="mt-2 flex items-center rounded-xl border border-white/10 bg-black px-4 focus-within:border-white/30">
              <span className="font-mono text-sm text-zinc-600">@</span>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))}
                maxLength={20}
                placeholder="heliophile"
                className="w-full bg-transparent px-1 py-2.5 font-mono text-sm text-white placeholder:text-zinc-700 focus:outline-none"
              />
            </div>
            <p className="mt-2 font-mono text-[11px] text-zinc-600">3–20 chars · lowercase, numbers, underscore · visible at /{username || "you"}</p>
          </div>
        )}

        {step === 1 && (
          <div>
            <h2 className="mt-6 text-2xl font-semibold tracking-tight">What do you do?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">So we size the advice to your building, not a generic home.</p>
            <p className="mt-6 text-[13px] text-zinc-400">Occupation</p>
            <div className="mt-2 flex flex-wrap gap-2">
              {OCCUPATIONS.map((o) => (
                <button
                  key={o}
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
            <div className="mt-2 flex flex-wrap gap-2">
              {PLACES.map((p) => (
                <button
                  key={p}
                  onClick={() => setPlace(p)}
                  className={`cursor-pointer rounded-full border px-4 py-2 text-[13px] transition active:scale-[0.96] ${
                    place === p ? "border-lime-300 bg-lime-300 text-black font-medium" : "border-white/15 text-zinc-400 hover:border-white/40 hover:text-white"
                  }`}
                >
                  {p}
                </button>
              ))}
            </div>
            <label className="mt-6 block text-[13px] text-zinc-400">Rooms (optional)</label>
            <input value={rooms} onChange={(e) => setRooms(e.target.value.replace(/[^0-9]/g, "").slice(0, 4))} inputMode="numeric" placeholder="e.g. 40" className={`mt-2 ${inputCls}`} />
          </div>
        )}

        {step === 2 && (
          <div>
            <h2 className="mt-6 text-2xl font-semibold tracking-tight">Which loads should we optimize?</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">Add what you have. You can add more any time from the dashboard.</p>
            <div className="mt-6 space-y-3">
              {drafts.map((d, i) => (
                <div key={i} className="rounded-2xl border border-white/10 p-4">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[11px] text-zinc-600">LOAD {i + 1}</span>
                    {drafts.length > 1 && (
                      <button onClick={() => setDrafts((ds) => ds.filter((_, j) => j !== i))} className="cursor-pointer font-mono text-[11px] text-zinc-600 transition hover:text-red-300">
                        remove
                      </button>
                    )}
                  </div>
                  <input value={d.name} onChange={(e) => setDraft(i, { name: e.target.value })} placeholder="Name anything — e.g. hostel borewell pump" maxLength={40} className={`mt-3 ${inputCls}`} />
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
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    <div className="flex items-center rounded-xl border border-white/10 bg-black px-3 focus-within:border-white/30">
                      <input value={d.powerKw} onChange={(e) => setDraft(i, { powerKw: e.target.value.replace(/[^0-9.]/g, "") })} inputMode="decimal" placeholder="kW" className="w-full bg-transparent py-2.5 text-sm text-white placeholder:text-zinc-700 focus:outline-none" />
                      <span className="font-mono text-[11px] text-zinc-600">kW</span>
                    </div>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    <label className="rounded-xl border border-white/10 bg-black px-3 py-2">
                      <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">ready by</span>
                      <input type="time" value={d.readyBy} onChange={(e) => setDraft(i, { readyBy: e.target.value })} className="w-full cursor-pointer bg-transparent text-sm text-white focus:outline-none [color-scheme:dark]" />
                    </label>
                    <label className="rounded-xl border border-white/10 bg-black px-3 py-2">
                      <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">flexible +{d.flexHours}h</span>
                      <input type="range" min={0} max={6} value={d.flexHours} onChange={(e) => setDraft(i, { flexHours: Number(e.target.value) })} className="w-full cursor-pointer accent-lime-300" />
                    </label>
                  </div>
                </div>
              ))}
            </div>
            <button
              onClick={() => setDrafts((ds) => [...ds, { name: "", powerKw: "", readyBy: "18:00", flexHours: 2 }])}
              className="mt-3 w-full cursor-pointer rounded-xl border border-dashed border-white/15 py-3 text-sm text-zinc-400 transition hover:border-white/40 hover:text-white active:scale-[0.99]"
            >
              + Add another load
            </button>
          </div>
        )}

        {step === 3 && (
          <div>
            <h2 className="mt-6 text-2xl font-semibold tracking-tight">Here is your order.</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">
              @{username.trim().toLowerCase()} · {occupation} · {place}
              {rooms ? ` · ${rooms} rooms` : ""} — ranked by priority, then optimized.
            </p>
            <ol className="mt-6 space-y-2">
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
          </div>
        )}

        {err && <p className="mt-4 font-mono text-[12px] text-orange-300">{err}</p>}

        <div className="mt-7 flex items-center justify-between">
          {step > 0 ? (
            <button onClick={() => { setErr(""); setStep(step - 1); }} className="cursor-pointer text-sm text-zinc-500 transition hover:text-white">
              ← Back
            </button>
          ) : (
            <span />
          )}
          {step < 3 ? (
            <button
              onClick={() => { setErr(""); setStep(step + 1); }}
              disabled={!canNext()}
              className="cursor-pointer rounded-full bg-white px-7 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-30"
            >
              Continue
            </button>
          ) : (
            <button
              onClick={finish}
              disabled={working || jobs.length === 0}
              className="flex cursor-pointer items-center gap-2 rounded-full bg-lime-300 px-8 py-2.5 text-sm font-medium text-black transition hover:bg-lime-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
            >
              {working && <span className="spinner" />}
              {working ? "Optimizing…" : "Optimize"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
