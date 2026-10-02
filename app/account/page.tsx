"use client";

import { useEffect, useState } from "react";
import { deleteUser, onAuthStateChanged, reauthenticateWithPopup, signOut, updateProfile, type User } from "firebase/auth";
import { deleteDoc, doc, getDoc, runTransaction, serverTimestamp, setDoc } from "firebase/firestore";
import { getDb, getFirebaseAuth, getGoogleProvider, isFirebaseConfigured } from "../../lib/firebase";

type Status = { kind: "idle" | "ok" | "err"; text: string };

function validUsername(v: string) {
  return /^[a-z0-9_]{3,20}$/.test(v);
}

export default function Account() {
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

  useEffect(() => {
    if (!isFirebaseConfigured()) {
      setReady(true);
      return;
    }
    const auth = getFirebaseAuth();
    if (!auth) {
      setReady(true);
      return;
    }
    return onAuthStateChanged(auth, async (u) => {
      setUser(u);
      setReady(true);
      if (u) {
        setName(u.displayName ?? "");
        const db = getDb();
        if (db) {
          const snap = await getDoc(doc(db, "users", u.uid));
          const un = snap.exists() ? (snap.data().username as string | undefined) : undefined;
          setCurrentUsername(un ?? null);
          setUsername(un ?? "");
        }
      }
    });
  }, []);

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
            <a href="/" className="text-zinc-500 transition hover:text-white">← back home</a>
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
      await updateProfile(user!, { displayName: v });
      const db = getDb();
      if (db) await setDoc(doc(db, "users", user!.uid), { displayName: v, updatedAt: serverTimestamp() }, { merge: true });
      setNameStatus({ kind: "ok", text: "Name updated." });
    } catch {
      setNameStatus({ kind: "err", text: "Couldn't save. Try again." });
    } finally {
      setSaving(false);
    }
  }

  async function claimUsername() {
    setUserStatus({ kind: "idle", text: "" });
    const v = username.trim().toLowerCase();
    if (!validUsername(v)) {
      setUserStatus({ kind: "err", text: "3–20 chars: lowercase letters, numbers, underscore." });
      return;
    }
    const db = getDb();
    if (!db) {
      setUserStatus({ kind: "err", text: "Database not configured." });
      return;
    }
    setChecking(true);
    try {
      await runTransaction(db, async (tx) => {
        const claimRef = doc(db, "usernames", v);
        const claim = await tx.get(claimRef);
        if (claim.exists() && claim.data().uid !== user!.uid) throw new Error("taken");
        const userRef = doc(db, "users", user!.uid);
        const prev = currentUsername && currentUsername !== v ? doc(db, "usernames", currentUsername) : null;
        tx.set(claimRef, { uid: user!.uid, updatedAt: serverTimestamp() });
        tx.set(userRef, { username: v, displayName: user!.displayName ?? null, email: user!.email ?? null, photoURL: user!.photoURL ?? null, updatedAt: serverTimestamp() }, { merge: true });
        if (prev) tx.delete(prev);
      });
      setCurrentUsername(v);
      setUserStatus({ kind: "ok", text: `@${v} is yours.` });
    } catch (e) {
      const code = e instanceof Error && "code" in e ? (e as { code?: string }).code : undefined;
      const msg = e instanceof Error ? e.message : "";
      if (msg === "taken") {
        setUserStatus({ kind: "err", text: `@${v} is taken. Try another.` });
      } else if (code === "permission-denied") {
        setUserStatus({ kind: "err", text: "Firestore rules are blocking this — apply the rules from chat, then retry." });
      } else if (code === "not-found" || /does not exist/i.test(msg)) {
        setUserStatus({ kind: "err", text: "Firestore database doesn't exist yet — create it in Firebase Console → Firestore, then retry." });
      } else {
        setUserStatus({ kind: "err", text: `Couldn't claim (${code ?? "unknown"}). Try again.` });
      }
    } finally {
      setChecking(false);
    }
  }

  async function handleDelete() {
    setDeleteStatus({ kind: "idle", text: "" });
    const auth = getFirebaseAuth();
    const db = getDb();
    if (!auth?.currentUser) return;
    setDeleting(true);
    try {
      try {
        await deleteUser(auth.currentUser);
      } catch (e: unknown) {
        if (e instanceof Error && (e as { code?: string }).code === "auth/requires-recent-login") {
          await reauthenticateWithPopup(auth.currentUser, getGoogleProvider());
          await deleteUser(auth.currentUser);
        } else throw e;
      }
      if (db) {
        if (currentUsername) await deleteDoc(doc(db, "usernames", currentUsername)).catch(() => {});
        await deleteDoc(doc(db, "users", auth.currentUser?.uid ?? user!.uid)).catch(() => {});
      }
      window.location.href = "/";
    } catch {
      setDeleteStatus({ kind: "err", text: "Deletion needs a fresh sign-in — it failed. Try again." });
      setDeleting(false);
    }
  }

  return (
    <main className="min-h-screen bg-black text-zinc-100">
      <div className="mx-auto max-w-3xl px-6 py-6 sm:px-10">
        <header className="flex items-center justify-between">
          <a href="/" className="text-[13px] font-semibold uppercase tracking-[0.28em]">Heliotrope</a>
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
                  <a
                    href="/dashboard"
                    className="block px-4 py-2.5 text-[13px] text-zinc-300 transition hover:bg-white/5 hover:text-white"
                  >
                    Dashboard
                  </a>
                  <button
                    onClick={async () => {
                      const auth = getFirebaseAuth();
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
        <section className="mt-10 rounded-2xl border border-white/10 bg-[#0a0a0a] p-6 sm:p-8">
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
              <p className="font-mono text-[12px] text-zinc-500">{currentUsername ? `@${currentUsername}` : "no username yet"}</p>
            </div>
          </div>

          <div className="mt-7">
            <label className="text-[13px] text-zinc-400">Display name</label>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              <input
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
            <label className="text-[13px] text-zinc-400">Username — unique across Heliotrope</label>
            <div className="mt-2 flex flex-col gap-2 sm:flex-row">
              <div className="flex flex-1 items-center rounded-xl border border-white/10 bg-black px-4 focus-within:border-white/30">
                <span className="font-mono text-sm text-zinc-600">@</span>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, ""))}
                  maxLength={20}
                  placeholder="heliophile"
                  className="w-full bg-transparent px-1 py-2.5 font-mono text-sm text-white placeholder:text-zinc-700 focus:outline-none"
                />
              </div>
              <button
                onClick={claimUsername}
                disabled={checking || saving}
                className="flex cursor-pointer items-center justify-center gap-2 rounded-xl bg-white px-5 py-2.5 text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60"
              >
                {checking && <span className="spinner" />}
                {checking ? "Checking…" : currentUsername ? "Change" : "Claim"}
              </button>
            </div>
            {userStatus.text && <p className={`mt-2 font-mono text-[12px] ${userStatus.kind === "ok" ? "text-lime-300" : "text-orange-300"}`}>{userStatus.text}</p>}
            {currentUsername && (
              <p className="mt-2 text-[13px] text-zinc-500">
                Public page: <a href={`/${currentUsername}`} className="font-mono text-[12px] text-zinc-200 underline decoration-white/20 underline-offset-4 hover:decoration-white/60">/{currentUsername}</a>
              </p>
            )}
          </div>
        </section>

        {/* danger */}
        <section className="mt-3 rounded-2xl border border-red-500/25 bg-red-500/[0.03] p-6 sm:p-8">
          <h2 className="text-[15px] font-medium text-red-300">Delete account</h2>
          <p className="mt-2 text-sm leading-6 text-zinc-500">
            Permanently removes your profile, username{currentUsername ? ` (@${currentUsername})` : ""} and sign-in.
            This can't be undone.
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
          {deleteStatus.text && <p className="mt-2 font-mono text-[12px] text-orange-300">{deleteStatus.text}</p>}
        </section>

        <p className="mt-10 pb-10 text-center text-[13px] italic text-red-400/90">
          Email or password changes aren't supported — Google is our only sign-in, so your email stays the one your Google account uses.
        </p>
      </div>
    </main>
  );
}
