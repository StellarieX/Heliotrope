import { deleteField, doc, updateDoc } from "firebase/firestore";
import type { Firestore } from "firebase/firestore";

/**
 * Profile documents are world-readable (the public /[username] page needs
 * them), so they must never hold an email. Earlier versions wrote one; strip it
 * the next time the owner opens the app. Best-effort and silent: if it fails
 * the owner just retries on the next visit.
 */
export async function scrubLegacyEmail(
  db: Firestore,
  uid: string,
  profile: Record<string, unknown> | null
): Promise<void> {
  if (!profile || !("email" in profile)) return;
  try {
    await updateDoc(doc(db, "users", uid), { email: deleteField() });
  } catch {
    /* retried on the next visit */
  }
}
