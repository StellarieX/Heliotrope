"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getSupabase } from "../../../lib/supabase";

export default function AuthCallback() {
  const [error, setError] = useState("");
  useEffect(() => {
    const client = getSupabase();
    if (!client) { queueMicrotask(() => setError("Sign-in is not configured.")); return; }
    const parameters = new URLSearchParams(window.location.search);
    if (parameters.has("error") || parameters.has("error_code")) {
      queueMicrotask(() => setError("Sign-in could not be completed. Please try again."));
      return;
    }
    let cancelled = false;
    // The client initializes its PKCE callback before getSession resolves.
    void client.auth.getSession().then(({ data, error: authError }) => {
      if (cancelled) return;
      if (authError || !data.session) { setError("Sign-in could not be completed. Please try again."); return; }
      const next = parameters.get("next");
      window.location.replace(next === "account" ? "/account" : "/dashboard");
    });
    return () => { cancelled = true; };
  }, []);
  return <main className="grid min-h-screen place-items-center bg-black text-zinc-300">{error ? <p role="alert">{error} <Link href="/" className="underline">Return home</Link></p> : <p>Completing sign-in…</p>}</main>;
}
