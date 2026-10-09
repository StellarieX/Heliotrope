"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { doc, getDoc } from "firebase/firestore";
import { getDb } from "../../lib/firebase";
import { validUsername } from "../../lib/username";

type Profile = { username: string; displayName: string | null; photoURL: string | null };

// A result belongs to the name it was loaded for. Navigating from /alice to /bob
// reuses this component, so a result for another name must never be shown.
type Result =
  | { name: string; kind: "found"; profile: Profile }
  | { name: string; kind: "missing" }
  | { name: string; kind: "error"; reason: "unavailable" | "failed" };

export default function PublicProfile({ params }: { params: Promise<{ username: string }> }) {
  const { username } = use(params);
  const name = username.toLowerCase();
  const nameValid = validUsername(name);
  const [db] = useState(() => getDb());
  const [result, setResult] = useState<Result | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!nameValid) return;
    if (!db) {
      // No database configured on this deployment: the profile can never load.
      // Deferred; the server and client must render the same first frame.
      queueMicrotask(() => setResult({ name, kind: "error", reason: "unavailable" }));
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const claim = await getDoc(doc(db, "usernames", name));
        const uid = claim.exists() ? claim.data().uid : null;
        if (typeof uid !== "string") {
          if (!cancelled) setResult({ name, kind: "missing" });
          return;
        }
        const snap = await getDoc(doc(db, "users", uid));
        if (cancelled) return;
        if (!snap.exists()) {
          setResult({ name, kind: "missing" });
          return;
        }
        const d = snap.data();
        setResult({
          name,
          kind: "found",
          profile: {
            username: name,
            displayName: typeof d.displayName === "string" ? d.displayName : null,
            photoURL: typeof d.photoURL === "string" ? d.photoURL : null,
          },
        });
      } catch {
        // A network or permission failure is not "nobody here": say what happened.
        if (!cancelled) setResult({ name, kind: "error", reason: "failed" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [db, name, nameValid, attempt]);

  const current: Result | null = !nameValid ? { name, kind: "missing" } : result && result.name === name ? result : null;

  return (
    <main className="grid min-h-screen place-items-center bg-black px-6 text-center text-zinc-100">
      {current?.kind === "found" ? (
        <div>
          {current.profile.photoURL ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={current.profile.photoURL} alt="" referrerPolicy="no-referrer" className="mx-auto h-20 w-20 rounded-full bg-white/10 object-cover" />
          ) : (
            <span className="mx-auto grid h-20 w-20 place-items-center rounded-full bg-lime-300 text-3xl font-bold text-black">
              {(current.profile.displayName ?? current.profile.username)[0]?.toUpperCase()}
            </span>
          )}
          <h1 className="mt-5 text-3xl font-semibold tracking-tight">{current.profile.displayName ?? `@${current.profile.username}`}</h1>
          <p className="mt-2 font-mono text-[13px] text-zinc-500">@{current.profile.username}</p>
          <p className="mt-8">
            <Link href="/" className="inline-flex min-h-11 items-center font-mono text-[12px] text-zinc-600 transition hover:text-white">heliotrope</Link>
          </p>
        </div>
      ) : current?.kind === "missing" ? (
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Nobody here yet</h1>
          <p className="mt-3 break-all font-mono text-[13px] text-zinc-500">
            {nameValid ? `@${name} isn't claimed.` : "That isn't a valid username."}
          </p>
          <p className="mt-8">
            <Link href="/" className="inline-flex min-h-11 items-center font-mono text-[12px] text-zinc-600 transition hover:text-white">← home</Link>
          </p>
        </div>
      ) : current?.kind === "error" ? (
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Couldn&apos;t load this page</h1>
          <p className="mt-3 max-w-sm font-mono text-[13px] leading-6 text-zinc-500">
            {current.reason === "unavailable"
              ? "Profiles aren't available on this deployment."
              : `We couldn't reach the database to look up @${name}. That doesn't mean the name is free.`}
          </p>
          <p className="mt-8 flex items-center justify-center gap-6">
            {current.reason === "failed" && (
              <button
                onClick={() => {
                  setResult(null);
                  setAttempt((n) => n + 1);
                }}
                className="min-h-11 cursor-pointer font-mono text-[12px] text-zinc-300 underline underline-offset-4 transition hover:text-white"
              >
                Try again
              </button>
            )}
            <Link href="/" className="inline-flex min-h-11 items-center font-mono text-[12px] text-zinc-600 transition hover:text-white">← home</Link>
          </p>
        </div>
      ) : (
        <p className="font-mono text-[12px] text-zinc-500">loading…</p>
      )}
    </main>
  );
}
