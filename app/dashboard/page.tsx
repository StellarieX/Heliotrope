"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { onAuthStateChanged, signInWithPopup, signOut, type User } from "firebase/auth";
import { addDoc, collection, deleteDoc, doc, getDoc, getDocs } from "firebase/firestore";
import { getDb, getFirebaseAuth, getGoogleProvider } from "../../lib/firebase";
import { jevRank, classifyJob, type JobInput, type RankedJob } from "../../lib/prioritize";
import { getCarbonSignal } from "../../lib/api/client";
import type { CarbonSignalResponse } from "../../lib/api/types";
import CarbonChart from "./CarbonChart";
import Onboarding from "./Onboarding";

type Profile = { username?: string; occupation?: string; place?: string; rooms?: number | null; onboarded?: boolean };

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

export default function Dashboard() {
  const [auth] = useState(() => getFirebaseAuth());
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(() => getFirebaseAuth() === null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileLoaded, setProfileLoaded] = useState(false);
  const [jobs, setJobs] = useState<JobInput[]>([]);
  const [ranked, setRanked] = useState<RankedJob[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [fName, setFName] = useState("");
  const [fPower, setFPower] = useState("");
  const [fReady, setFReady] = useState("06:00");
  const [fFlex, setFFlex] = useState(2);
  const [signal, setSignal] = useState<CarbonSignalResponse | null>(null);
  const [signalError, setSignalError] = useState<string | null>(null);

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
      setJobs(js.docs.map((d) => ({ id: d.id, ...(d.data() as Omit<JobInput, "id">) })));
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

  useEffect(() => {
    const end = new Date();
    const start = new Date(end.getTime() - 24 * 3600 * 1000);
    getCarbonSignal({ start: start.toISOString(), end: end.toISOString() })
      .then(setSignal)
      .catch(() => setSignalError("Backend signal unreachable — is it running?"));
  }, []);

  async function addJob() {
    if (!user || !fName.trim() || !fReady) return;
    const db = getDb();
    if (!db) return;
    setAdding(true);
    const c = classifyJob(fName);
    try {
      const ref = await addDoc(collection(db, "users", user.uid, "jobs"), {
        name: fName.trim(),
        kind: c.category,
        shiftable: c.shiftable,
        powerKw: Number(fPower) || 0,
        readyBy: fReady,
        flexHours: fFlex,
        createdAt: new Date().toISOString(),
      });
      setJobs((js) => [...js, { id: ref.id, name: fName.trim(), kind: c.category, shiftable: c.shiftable, powerKw: Number(fPower) || 0, readyBy: fReady, flexHours: fFlex }]);
      setRanked(null);
      setFName("");
    } finally {
      setAdding(false);
    }
  }

  async function removeJob(id: string) {
    if (!user) return;
    const db = getDb();
    if (!db) return;
    await deleteDoc(doc(db, "users", user.uid, "jobs", id));
    setJobs((js) => js.filter((j) => j.id !== id));
    setRanked(null);
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
          your loads, ranked by priority, then optimized.
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
              Optimize
            </button>
          </div>

          {(ranked ? [...ranked, ...jobs.filter((j) => j.shiftable === false)] : jobs).map((j, i) => (
            <div key={j.id} className="group flex items-center gap-5 border-t border-white/5 px-6 py-5 transition hover:bg-white/[0.02] sm:px-8">
              {ranked && j.shiftable !== false && <span className="w-6 shrink-0 font-mono text-[13px] text-zinc-600">{String(i + 1).padStart(2, "0")}</span>}
              <KindIcon kind={j.kind} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <p className="text-[15px] font-medium">{j.name}</p>
                  {j.shiftable === false ? (
                    <span className="shrink-0 rounded-full bg-white/5 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">always-on · filtered</span>
                  ) : (
                    ranked && <BandChip band={(j as RankedJob).band} />
                  )}
                </div>
                <p className="mt-1.5 text-[13px] text-zinc-500">
                  {j.kind || "Load"} · {j.powerKw} kW · ready by {j.readyBy} · +{j.flexHours}h flexible
                </p>
                {ranked && j.shiftable !== false && (
                  <>
                    <div className="mt-2.5 h-1 max-w-md overflow-hidden rounded-full bg-white/10">
                      <div className="h-full rounded-full bg-lime-300 transition-[width] duration-700" style={{ width: `${(j as RankedJob).score}%` }} />
                    </div>
                    <p className="mt-1.5 text-[13px] text-zinc-500">{(j as RankedJob).reason}</p>
                  </>
                )}
              </div>
              <button onClick={() => void removeJob(j.id)} className="shrink-0 cursor-pointer font-mono text-[11px] text-zinc-700 transition hover:text-red-300 sm:opacity-0 sm:group-hover:opacity-100">
                remove
              </button>
            </div>
          ))}

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
                        {fName.trim() ? `${c.category} · ${c.why}` : "type a name — Jev files it"}
                      </span>
                    </>
                  );
                })()}
              </div>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-3">
              <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                <input value={fPower} onChange={(e) => setFPower(e.target.value.replace(/[^0-9.]/g, ""))} inputMode="decimal" placeholder="kW" className={`w-20 ${inputCls}`} />
                <span className="font-mono text-[11px] text-zinc-600">kW rating</span>
              </label>
              <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                <span className="font-mono text-[11px] text-zinc-600">ready by</span>
                <input type="time" value={fReady} onChange={(e) => setFReady(e.target.value)} className={`cursor-pointer [color-scheme:dark] ${inputCls}`} />
              </label>
              <label className="flex items-center gap-2 text-[13px] text-zinc-400">
                <span className="font-mono text-[11px] text-zinc-600">+{fFlex}h flex</span>
                <input type="range" min={0} max={6} value={fFlex} onChange={(e) => setFFlex(Number(e.target.value))} className="w-28 cursor-pointer accent-lime-300" />
              </label>
              <button
                onClick={() => void addJob()}
                disabled={adding || !fName.trim() || !fReady}
                className="flex cursor-pointer items-center gap-2 rounded-full bg-white px-6 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-30"
              >
                {adding && <span className="spinner" />}
                {adding ? "Adding…" : "Add load"}
              </button>
            </div>
          </div>
        </section>

        <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-6 sm:p-8">
          {signal ? (
            <CarbonChart signal={signal} />
          ) : signalError ? (
            <p className="font-mono text-[12px] text-zinc-600">{signalError}</p>
          ) : (
            <p className="font-mono text-[12px] text-zinc-600">reading grid signal…</p>
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
            <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Not built</p>
            <h2 className="mt-2 text-[15px] font-medium">Schedules</h2>
            <p className="mt-2 text-sm leading-6 text-zinc-500">Priority order is live above. Timed schedules arrive with the backend.</p>
          </div>
        </div>
      </div>
    </main>
  );
}
