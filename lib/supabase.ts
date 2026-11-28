import { createClient, type SupabaseClient, type User as SupabaseUser } from "@supabase/supabase-js";

export type User = { uid: string; displayName: string | null; photoURL: string | null; email: string | null };
let client: SupabaseClient | null = null;
export function getSupabase(): SupabaseClient | null {
  if (typeof window === "undefined") return null;
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) return null;
  return client ??= createClient(url, key, { auth: { flowType: "pkce" } });
}
function mapUser(u: SupabaseUser | null): User | null {
  if (!u) return null;
  const name = u.user_metadata?.full_name ?? u.user_metadata?.name;
  const photo = u.user_metadata?.avatar_url;
  return {
    uid: u.id,
    displayName: typeof name === "string" ? name.slice(0, 80) : null,
    photoURL: typeof photo === "string" && photo.startsWith("https://") && photo.length <= 2048 ? photo : null,
    email: u.email ?? null,
  };
}
export function onAuthStateChanged(auth: SupabaseClient, cb: (user: User | null) => void): () => void {
  let currentId: string | null | undefined;
  let notification: ReturnType<typeof setTimeout> | undefined;
  const { data } = auth.auth.onAuthStateChange((event, session) => {
    const user = mapUser(session?.user ?? null);
    const id = user?.uid ?? null;
    if (id === currentId && event !== "USER_UPDATED") return;
    currentId = id;
    clearTimeout(notification);
    // Leave Supabase's auth callback before starting profile or token reads.
    notification = setTimeout(() => cb(user), 0);
  });
  return () => {
    clearTimeout(notification);
    data.subscription.unsubscribe();
  };
}
export async function signIn(auth: SupabaseClient, destination: "dashboard" | "account" = "dashboard", reauthenticate = false) {
  const { error } = await auth.auth.signInWithOAuth({
    provider: "google",
    options: {
      redirectTo: `${window.location.origin}/auth/callback?next=${destination}`,
      ...(reauthenticate ? { queryParams: { prompt: "select_account" } } : {}),
    },
  });
  if (error) throw error;
}
export async function signOut(auth: SupabaseClient) {
  const { error } = await auth.auth.signOut({ scope: "local" });
  if (error) throw error;
}
