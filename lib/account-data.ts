import { getSupabase } from "./supabase";

function db() { const client = getSupabase(); if (!client) throw new Error("Database not configured."); return client; }
export async function getProfile(uid: string) {
  const { data, error } = await db().from("profiles").select("*").eq("id", uid).maybeSingle();
  if (error) throw error; return data;
}
export async function updateAccount(uid: string, patch: Record<string, unknown>) {
  const { data, error } = await db().from("profiles").update(patch).eq("id", uid).select("id").single();
  if (error) throw error; return data;
}
export async function getPublicProfile(username: string) {
  const { data, error } = await db().from("profile_directory").select("username, display_name, photo_url").eq("username", username).maybeSingle();
  if (error) throw error; return data;
}
export async function getJobs(uid: string) {
  const { data, error } = await db().from("jobs").select("id, spec").eq("owner_id", uid).order("created_at");
  if (error) throw error; return data;
}
export async function addJob(uid: string, spec: Record<string, unknown>) {
  const { data, error } = await db().from("jobs").insert({ owner_id: uid, spec }).select("id").single();
  if (error) throw error; return data;
}
export async function patchJob(id: string, patch: Record<string, unknown>) {
  const { data, error } = await db().rpc("patch_job", { p_job_id: id, p_patch: patch }); if (error) throw error; if (!data) throw new Error("Load no longer exists.");
}
export async function removeJob(uid: string, id: string) {
  const { error } = await db().from("jobs").delete().eq("owner_id", uid).eq("id", id); if (error) throw error;
}
export async function completeOnboarding(args: Record<string, unknown>) {
  const { error } = await db().rpc("complete_onboarding", args); if (error) throw error;
}
