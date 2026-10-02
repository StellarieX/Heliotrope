import { doc, runTransaction, serverTimestamp } from "firebase/firestore";
import type { Firestore } from "firebase/firestore";

export function validUsername(v: string) {
  return /^[a-z0-9_]{3,20}$/.test(v);
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
    tx.set(doc(db, "users", uid), { username: v, ...profile, updatedAt: serverTimestamp() }, { merge: true });
    if (prev && prev !== v) tx.delete(doc(db, "usernames", prev));
  });
  return v;
}
