"use client";

import { use, useEffect, useState } from "react";
import { doc, getDoc } from "firebase/firestore";
import { getDb, isFirebaseConfigured } from "../../lib/firebase";

type Profile = { username: string; displayName: string | null; photoURL: string | null };

export default function PublicProfile({ params }: { params: Promise<{ username: string }> }) {
  const { username } = use(params);
  const name = username.toLowerCase();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    if (!isFirebaseConfigured()) {
      setMissing(true);
      return;
    }
    const db = getDb();
    if (!db) {
      setMissing(true);
      return;
    }
    (async () => {
      const claim = await getDoc(doc(db, "usernames", name));
      if (!claim.exists()) {
        setMissing(true);
        return;
      }
      const snap = await getDoc(doc(db, "users", claim.data().uid as string));
      if (!snap.exists()) {
        setMissing(true);
        return;
      }
      const d = snap.data();
      setProfile({ username: name, displayName: (d.displayName as string | null) ?? null, photoURL: (d.photoURL as string | null) ?? null });
    })().catch(() => setMissing(true));
  }, [name]);

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
            <a href="/" className="font-mono text-[12px] text-zinc-600 transition hover:text-white">heliotrope</a>
          </p>
        </div>
      ) : missing ? (
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">Nobody here yet</h1>
          <p className="mt-3 font-mono text-[13px] text-zinc-500">@{name} isn't claimed.</p>
          <p className="mt-8">
            <a href="/" className="font-mono text-[12px] text-zinc-600 transition hover:text-white">← home</a>
          </p>
        </div>
      ) : (
        <p className="font-mono text-[12px] text-zinc-500">loading…</p>
      )}
    </main>
  );
}
