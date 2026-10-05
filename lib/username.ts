import { doc, runTransaction, serverTimestamp } from "firebase/firestore";
import type { Firestore } from "firebase/firestore";

// Route segments owned by the app shell — claiming one as a username would
// make the public page unreachable (static routes win over [username]).
export const RESERVED_USERNAMES = new Set([
  "dashboard",
  "account",
  "api",
  "admin",
  "login",
  "signin",
  "signout",
  "settings",
  "help",
  "about",
  "null",
  "undefined",
]);

export function validUsername(v: string) {
  return /^[a-z0-9_]{3,20}$/.test(v) && !RESERVED_USERNAMES.has(v);
}

// Claims `username` for `uid`, releasing `prev` (if different).
// Throws Error("taken") when owned by someone else.
export async function claimUsername(db: Firestore, uid: string, username: string, prev: string | null, profile: Record<string, unknown>) {
  const v = username.trim().toLowerCase();
  if (!validUsername(v)) throw new Error("invalid");
  await runTransaction(db, async (tx) => {
    const claimRef = doc(db, "usernames", v);
    const claim = await tx.get(claimRef);
    if (claim.exists() && claim.data().uid !== uid) throw new Error("taken");
    tx.set(claimRef, { uid, updatedAt: serverTimestamp() });
    // Spread first so the claimed name always wins: a stale `username` inside
    // `profile` must never overwrite the name just claimed.
    tx.set(doc(db, "users", uid), { ...profile, username: v, updatedAt: serverTimestamp() }, { merge: true });
    if (prev && prev !== v) tx.delete(doc(db, "usernames", prev));
  });
  return v;
}
