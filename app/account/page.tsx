"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { getSupabase, onAuthStateChanged, signIn, signOut, type User } from "../../lib/supabase";
import { getProfile, updateAccount } from "../../lib/account-data";
import { deleteAccount } from "../../lib/api/client";
import { validUsername } from "../../lib/username";
import { OCCUPATIONS, PLACES, profileFor } from "../../lib/occupations";

type Status = { kind: "idle" | "ok" | "err"; text: string };

export default function Account() {
  const router = useRouter();
  const [auth] = useState(() => getSupabase());
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [currentUsername, setCurrentUsername] = useState<string | null>(null);
  const [nameStatus, setNameStatus] = useState<Status>({ kind: "idle", text: "" });
  const [userStatus, setUserStatus] = useState<Status>({ kind: "idle", text: "" });
  const [checking, setChecking] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteStatus, setDeleteStatus] = useState<Status>({ kind: "idle", text: "" });
  const [menuOpen, setMenuOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // The profile document loads after sign-in; until it has, username controls stay off
  // so a slow load can never be mistaken for "no username yet".
  const [profileState, setProfileState] = useState<"loading" | "ready" | "error">("loading");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [occupation, setOccupation] = useState("");
  const [place, setPlace] = useState("");
  const [maxPower, setMaxPower] = useState("");
  const [profileSaving, setProfileSaving] = useState(false);
  const [aboutStatus, setAboutStatus] = useState<Status>({ kind: "idle", text: "" });

  useEffect(() => {
    if (!auth) {
    // Supabase not configured: nothing will ever resolve, so stop showing "loading".
    // Deferred, and `ready` starts false on both server and client so they render the
    // same first frame (a different initial value caused a hydration mismatch).
    queueMicrotask(() => setReady(true));
    return;
    }
    let cancelled = false;
    let generation = 0;
    const unsub = onAuthStateChanged(auth, (u) => {
      const current = ++generation;
      setUser(u);
      setReady(true);
      setName(u?.displayName ?? "");
      setUsername(""); setCurrentUsername(null); setOccupation(""); setPlace(""); setMaxPower("");
      setProfileState("loading");
      if (!u) return;
      void getProfile(u.uid).then((data) => {
        if (cancelled || current !== generation) return;
        setName(data?.display_name ?? u.displayName ?? "");
        setCurrentUsername(data?.username ?? null);
        setUsername(data?.username ?? "");
        setOccupation(data?.occupation ?? ""); setPlace(data?.place ?? "");
        setMaxPower(data?.max_power_kw != null ? String(data.max_power_kw) : "");
        setProfileState("ready");
      }).catch(() => { if (!cancelled && current === generation) setProfileState("error"); });
    });
    return () => { cancelled = true; unsub(); };
  }, [auth, loadAttempt]);

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
          <h1 className="text-3xl font-semibold tracking-tight">Sign in to manage your account</h1>
          <p className="mt-6 text-[13px]">
            <Link href="/" className="text-zinc-500 transition hover:text-white">← back home</Link>
          </p>
        </div>
      </main>
    );
  }

  async function saveName() {
    setNameStatus({ kind: "idle", text: "" });
    const v = name.trim();
    if (v.length < 1 || v.length > 40) {
      setNameStatus({ kind: "err", text: "Name must be 1–40 characters." });
      return;
    }
    setSaving(true);
    try {
      await updateAccount(user!.uid, { display_name: v });
      setUser((old) => old ? { ...old, displayName: v } : old);
      setNameStatus({ kind: "ok", text: "Name updated." });
    } catch {
      setNameStatus({ kind: "err", text: "Couldn't save. Try again." });
    } finally { setSaving(false); }
  }

  async function claimUsername() {
    setUserStatus({ kind: "idle", text: "" });
    const v = username.trim().toLowerCase();
    if (!validUsername(v)) {
      setUserStatus({ kind: "err", text: "3–20 chars: lowercase letters, numbers, underscore." });
      return;
    }
    if (v === currentUsername) {
      setUserStatus({ kind: "ok", text: `@${v} is already yours.` });
      return;
    }
    setChecking(true);
    try {
      await updateAccount(user!.uid, { username: v });
      const old = currentUsername;
      setCurrentUsername(v);
      setUserStatus({ kind: "ok", text: old ? `@${v} is yours. /${old} no longer leads to your page.` : `@${v} is yours.` });
    } catch (e) {
      const code = (e as { code?: string }).code;
      setUserStatus({ kind: "err", text: code === "23505" ? `@${v} is taken. Try another.` : "Couldn't claim that username. Try again." });
    } finally { setChecking(false); }
  }

  async function saveAbout() {
    setAboutStatus({ kind: "idle", text: "" });
    const kw = maxPower === "" ? null : Number(maxPower);
    if (kw !== null && !(kw > 0 && kw <= 1000)) {
      setAboutStatus({ kind: "err", text: "Max power at once must be above 0 and at most 1000 kW." });
      return;
    }
    setProfileSaving(true);
    try {
      await updateAccount(user!.uid, { occupation, place, max_power_kw: kw });
      setAboutStatus({ kind: "ok", text: "Saved." });
    } catch {
      setAboutStatus({ kind: "err", text: "Couldn't save. Try again." });
    } finally { setProfileSaving(false); }
  }

  async function handleDelete() {
    setDeleteStatus({ kind: "idle", text: "" });
    const auth = getSupabase();
    if (!auth) return;
    setDeleting(true);
    try {
      await deleteAccount();
      await auth.auth.signOut({ scope: "local" });
      router.push("/");
    } catch (e) {
      const status = (e as { status?: number }).status;
      setDeleteStatus({ kind: "err", text: status === 401 || status === 403 ? "Sign in again with Google, then confirm deletion." : "Couldn't delete your account. Check your connection and try again." });
      setDeleting(false);
    }
  }

  return (
    <main className="min-h-screen bg-black text-zinc-100">
      <div className="mx-auto max-w-3xl px-4 py-4 sm:px-10 sm:py-6">
        <header className="flex items-center justify-between">
          <Link href="/" className="inline-flex min-h-11 items-center text-[13px] font-semibold uppercase tracking-[0.28em]">Heliotrope</Link>
          <div className="relative flex items-center gap-3">
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
                    href="/dashboard"
                    className="block px-4 py-2.5 text-[13px] text-zinc-300 transition hover:bg-white/5 hover:text-white"
                  >
                    Dashboard
                  </Link>
                  <button
                    onClick={async () => {
                      const auth = getSupabase();
                      if (auth) await signOut(auth);
                    }}
                    className="block w-full cursor-pointer px-4 py-2.5 text-left text-[13px] text-zinc-300 transition hover:bg-white/5 hover:text-white active:bg-white/10"
                  >
                    Sign out
                  </button>
                </div>
              </>
            )}
          </div>
        </header>

        <h1 className="mt-12 text-3xl font-semibold tracking-tight sm:text-4xl">Account</h1>
        <p className="mt-3 text-[15px] text-zinc-500">Signed in with Google as <span className="font-mono text-[13px] text-zinc-300">{user.email}</span></p>

        {/* profile */}
        <section className="mt-10 rounded-2xl border border-white/10 bg-[#0a0a0a] p-4 sm:p-8">
          <div className="flex items-center gap-4">
            {user.photoURL ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={user.photoURL} alt="" referrerPolicy="no-referrer" className="h-12 w-12 rounded-full bg-white/10 object-cover" />
            ) : (
              <span className="grid h-12 w-12 place-items-center rounded-full bg-lime-300 text-lg font-bold text-black">
                {(user.displayName ?? user.email ?? "H")[0]?.toUpperCase()}
              </span>
            )}
            <div>
              <p className="text-[15px] font-medium">{user.displayName ?? "No name set"}</p>
              <p className="font-mono text-[12px] text-zinc-500">
                {profileState === "loading" ? "loading…" : currentUsername ? `@${currentUsername}` : "no username yet"}
              </p>
            </div>
          </div>

          {profileState === "error" && (
            <p role="alert" className="mt-6 font-mono text-[12px] text-orange-300">
              Couldn&apos;t load your saved profile, so the username below may be out of date.{" "}
              <button onClick={() => setLoadAttempt((n) => n + 1)} className="cursor-pointer text-zinc-200 underline underline-offset-4 hover:text-white">
                Try again
              </button>
            </p>
          )}

          <div className="mt-7">
            <label htmlFor="acc-name" className="text-[13px] text-zinc-400">Display name</label>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              <input
                id="acc-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={40}
                placeholder="Your name"
                className="flex-1 rounded-xl border border-white/10 bg-black px-4 py-2.5 text-sm text-white placeholder:text-zinc-700 focus:border-white/30 focus:outline-none"
              />
              <button onClick={saveName} disabled={saving || checking} className="flex cursor-pointer items-center justify-center gap-2 rounded-xl bg-white px-5 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60">
                {saving && <span className="spinner" />}
                {saving ? "Saving…" : "Save"}
              </button>
            </div>
            {nameStatus.text && <p className={`mt-2 font-mono text-[12px] ${nameStatus.kind === "ok" ? "text-lime-300" : "text-orange-300"}`}>{nameStatus.text}</p>}
          </div>

          <div className="mt-7 border-t border-white/10 pt-7">
            <label htmlFor="acc-username" className="text-[13px] text-zinc-400">Username (unique across Heliotrope)</label>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              <div className="flex flex-1 items-center rounded-xl border border-white/10 bg-black px-4 focus-within:border-white/30">
                <span className="font-mono text-sm text-zinc-600">@</span>
                <input
                  id="acc-username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))}
                  maxLength={20}
                  placeholder="heliophile"
                  className="w-full bg-transparent px-1 py-2.5 font-mono text-sm text-white placeholder:text-zinc-700 focus:outline-none"
                />
              </div>
              <button
                onClick={claimUsername}
                disabled={checking || saving || profileState !== "ready"}
                className="flex cursor-pointer items-center justify-center gap-2 rounded-xl bg-white px-5 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
              >
                {checking && <span className="spinner" />}
                {checking ? "Checking…" : profileState === "loading" ? "Loading…" : currentUsername ? "Change" : "Claim"}
              </button>
            </div>
            {userStatus.text && <p className={`mt-2 font-mono text-[12px] ${userStatus.kind === "ok" ? "text-lime-300" : "text-orange-300"}`}>{userStatus.text}</p>}
            {currentUsername && (
              <p className="mt-2 text-[13px] text-zinc-500">
                Public page: <Link href={`/${currentUsername}`} className="inline-block py-2 font-mono text-[12px] text-zinc-200 underline decoration-white/20 underline-offset-4 hover:decoration-white/60">/{currentUsername}</Link>
              </p>
            )}
          </div>
        </section>

        {/* about: occupation, place, max power */}
        <section className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-4 sm:p-8">
          <h2 className="text-[15px] font-medium">Your setup</h2>
          <p className="mt-2 text-sm leading-6 text-zinc-500">
            These choose which appliances are offered first and what we suggest for the most power your site can draw at once.
            Occupation and place are saved in your profile record, which the database allows anyone to read.
          </p>
          <p className="mt-5 text-[13px] text-zinc-400">Occupation</p>
          <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Occupation">
            {OCCUPATIONS.map((o) => (
              <button
                key={o}
                type="button"
                role="radio"
                aria-checked={occupation === o}
                onClick={() => setOccupation(o)}
                className={`min-h-11 cursor-pointer rounded-full border px-4 py-2 text-[13px] transition active:scale-[0.96] ${
                  occupation === o ? "border-lime-300 bg-lime-300 font-medium text-black" : "border-white/15 text-zinc-400 hover:border-white/40 hover:text-white"
                }`}
              >
                {o}
              </button>
            ))}
          </div>
          <p className="mt-5 text-[13px] text-zinc-400">Where this runs</p>
          <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Place">
            {PLACES.map((p) => (
              <button
                key={p}
                type="button"
                role="radio"
                aria-checked={place === p}
                onClick={() => setPlace(p)}
                className={`min-h-11 cursor-pointer rounded-full border px-4 py-2 text-[13px] transition active:scale-[0.96] ${
                  place === p ? "border-lime-300 bg-lime-300 font-medium text-black" : "border-white/15 text-zinc-400 hover:border-white/40 hover:text-white"
                }`}
              >
                {p}
              </button>
            ))}
          </div>
          <label htmlFor="acc-maxpower" className="mt-5 block text-[13px] text-zinc-400">Max power at once (kW)</label>
          <input
            id="acc-maxpower"
            value={maxPower}
            onChange={(e) => setMaxPower(e.target.value.replace(/[^0-9.]/g, ""))}
            inputMode="decimal"
            placeholder={occupation && place ? `Suggested: ${profileFor(occupation, place).capacityKw}` : "e.g. 10"}
            className="mt-2 w-full rounded-xl border border-white/10 bg-black px-4 py-2.5 text-sm text-white placeholder:text-zinc-700 focus:border-white/30 focus:outline-none sm:w-48"
          />
          <p className="mt-2 font-mono text-[11px] leading-5 text-zinc-600">Kept in this browser only. Set it to your sanctioned load or breaker rating.</p>
          <div className="mt-4 flex items-center gap-3">
            <button
              onClick={saveAbout}
              disabled={profileSaving || profileState !== "ready"}
              className="flex cursor-pointer items-center justify-center gap-2 rounded-xl bg-white px-5 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
            >
              {profileSaving && <span className="spinner" />}
              {profileSaving ? "Saving…" : "Save"}
            </button>
            {aboutStatus.text && <p className={`font-mono text-[12px] ${aboutStatus.kind === "ok" ? "text-lime-300" : "text-orange-300"}`}>{aboutStatus.text}</p>}
          </div>
        </section>

        {/* danger */}
        <section className="mt-3 rounded-2xl border border-red-500/25 bg-red-500/[0.03] p-4 sm:p-8">
          <h2 className="text-[15px] font-medium text-red-300">Delete account</h2>
          <p className="mt-2 text-sm leading-6 text-zinc-500">
            Permanently removes your profile, username{currentUsername ? ` (@${currentUsername})` : ""} and sign-in.
            This can&apos;t be undone. Sign in again with Google before confirming deletion.
          </p>
          {!confirmDelete ? (
            <button
              onClick={() => setConfirmDelete(true)}
              className="mt-4 cursor-pointer rounded-xl border border-red-500/40 px-5 py-2.5 text-sm text-red-300 transition hover:bg-red-500/10 active:scale-[0.98]"
            >
              Delete my account
            </button>
          ) : (
            <div className="mt-4 flex flex-col gap-2 sm:flex-row">
              <button onClick={handleDelete} disabled={deleting} className="flex cursor-pointer items-center justify-center gap-2 rounded-xl bg-red-500 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-red-400 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60">
                {deleting && <span className="spinner spinner-light" />}
                {deleting ? "Deleting…" : "Yes, delete everything"}
              </button>
              <button onClick={() => setConfirmDelete(false)} disabled={deleting} className="cursor-pointer rounded-xl border border-white/15 px-5 py-2.5 text-sm text-zinc-300 transition hover:border-white/40 active:scale-[0.98] disabled:opacity-60">
                Keep my account
              </button>
            </div>
          )}
          <button onClick={() => { const a = getSupabase(); if (a) void signIn(a, "account", true).catch(() => setDeleteStatus({ kind: "err", text: "Couldn't start Google sign-in. Try again." })); }} className="mt-4 block text-sm text-red-300 underline">Sign in again with Google</button>
          {deleteStatus.text && <p className="mt-2 font-mono text-[12px] text-orange-300">{deleteStatus.text}</p>}
        </section>

        <p className="mt-10 pb-10 text-center text-[13px] italic text-red-400/90">
          Email or password changes aren&apos;t supported — Google is our only sign-in, so your email stays the one your Google account uses.
        </p>
      </div>
    </main>
  );
}
