"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { doc, getDoc } from "firebase/firestore";
import { getDb } from "../../lib/firebase";
import { validUsername } from "../../lib/username";

type Profile = { username: string; displayName: string | null; photoURL: string | null };

export default function PublicProfile({ params }: { params: Promise<{ username: string }> }) {
  const { username } = use(params);
  const name = username.toLowerCase();
  const nameValid = validUsername(name);
  const [db] = useState(() => getDb());
  const [profile, setProfile] = useState<Profile | null>(null);
  const [missing, setMissing] = useState(() => getDb() === null || !validUsername(username.toLowerCase()));

  useEffect(() => {
    if (!db || !nameValid) return;
    let cancelled = false;
    (async () => {
      try {
        const claim = await getDoc(doc(db, "usernames", name));
        if (!claim.exists()) {
          if (!cancelled) setMissing(true);
          return;
        }
        const snap = await getDoc(doc(db, "users", claim.data().uid as string));
        if (!snap.exists()) {
          if (!cancelled) setMissing(true);
          return;
        }
        if (cancelled) return;
        const d = snap.data();
        setProfile({ username: name, displayName: (d.displayName as string | null) ?? null, photoURL: (d.photoURL as string | null) ?? null });
      } catch {
        if (!cancelled) setMissing(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [db, name, nameValid]);

  return (
    <main className="grid min-h-screen place-items-center bg-black px-6 text-center text-zinc-100">
      {profile ? (
        <div>
          {profile.photoURL ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={profile.photoURL} alt="" referrerPolicy="no-referrer" className="mx-auto h-20 w-20 rounded-full bg-white/10 object-cover" />
          ) : (
            <span className="mx-auto grid h-20 w-20 place-items-center rounded-full bg-lime-300 text-3xl font-bold text-black">
              {(profile.displayName ?? "H")[0]?.toUpperCase()}
            </span>
          )}
          <h1 className="mt-5 text-3xl font-semibold tracking-tight">{profile.displayName ?? `@${profile.username}`}</h1>
          <p className="mt-2 font-mono text-[13px] text-zinc-500">@{profile.username}</p>
          <p className="mt-8">
            <Link href="/" className="font-mono text-[12px] text-zinc-600 transition hover:text-white">heliotrope</Link>
          </p>
        </div>
      ) : missing ? (
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Nobody here yet</h1>
          <p className="mt-3 font-mono text-[13px] text-zinc-500">@{name} isn&apos;t claimed.</p>
          <p className="mt-8">
            <Link href="/" className="font-mono text-[12px] text-zinc-600 transition hover:text-white">← home</Link>
          </p>
        </div>
      ) : (
        <p className="font-mono text-[12px] text-zinc-500">loading…</p>
      )}
    </main>
  );
}
