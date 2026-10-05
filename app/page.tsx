"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { onAuthStateChanged, signInWithPopup, type User } from "firebase/auth";
import { getFirebaseAuth, getGoogleProvider } from "../lib/firebase";
import LivePlan from "./LivePlan";

function Avatar({ name, email, photoURL }: { name: string | null; email: string | null; photoURL: string | null }) {
  const [broken, setBroken] = useState(false);
  if (photoURL && !broken) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={photoURL}
        alt=""
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
        className="h-7 w-7 rounded-full bg-white/10 object-cover"
      />
    );
  }
  return (
    <span className="grid h-7 w-7 place-items-center rounded-full bg-lime-300 text-[12px] font-bold text-black">
      {(name ?? email ?? "H")[0]?.toUpperCase()}
    </span>
  );
}

export default function Home() {
  const router = useRouter();
  const [auth] = useState(() => getFirebaseAuth());
  const [user, setUser] = useState<User | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  const [navigating, setNavigating] = useState(false);
  const navTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function scrollToSection(hash: string) {
    return (e: React.MouseEvent<HTMLAnchorElement>) => {
      e.preventDefault();
      const el = document.querySelector(hash);
      if (!el) return;
      if (navTimer.current) clearTimeout(navTimer.current);
      setNavigating(false);
      // Force reflow so the motion-blur pulse replays on repeat clicks.
      void document.body.offsetHeight;
      setNavigating(true);
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      navTimer.current = setTimeout(() => setNavigating(false), 600);
    };
  }

  useEffect(() => {
    return () => {
      if (navTimer.current) clearTimeout(navTimer.current);
    };
  }, []);

  useEffect(() => {
    if (!auth) return;
    return onAuthStateChanged(auth, (u) => {
      setUser(u);
      if (u) {
        router.push("/dashboard");
      }
    });
  }, [auth, router]);

  useEffect(() => {
    const els = Array.from(document.querySelectorAll(".js-reveal"));
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            io.unobserve(e.target);
          }
        }
      },
      { threshold: 0.12 }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  async function handleSignIn() {
    setAuthError(null);
    if (!auth) {
      setAuthError("Add Firebase keys to .env.local first.");
      return;
    }
    setSigningIn(true);
    try {
      await signInWithPopup(auth, getGoogleProvider());
      router.push("/dashboard");
    } catch {
      setAuthError("Sign-in failed. Try again.");
      setSigningIn(false);
    }
  }

  function handleAccountClick() {
    if (user) router.push("/dashboard");
  }

  return (
    <main className="min-h-screen bg-black text-zinc-100 antialiased selection:bg-lime-300 selection:text-black">
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0"
        style={{
          backgroundImage:
            "linear-gradient(rgba(255,255,255,0.032) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.032) 1px, transparent 1px)",
          backgroundSize: "80px 80px",
          maskImage: "radial-gradient(ellipse 80% 55% at 50% 0%, black 25%, transparent 75%)",
          WebkitMaskImage: "radial-gradient(ellipse 80% 55% at 50% 0%, black 25%, transparent 75%)",
        }}
      />
      <div className={`relative${navigating ? " nav-motion" : ""}`}>
        {/* nav */}
        <header className="mx-auto flex max-w-7xl items-center justify-between px-6 py-6 sm:px-10 lg:px-16">
          <div className="flex items-center gap-2.5">
            <span className="text-[13px] font-semibold uppercase tracking-[0.28em]">Heliotrope</span>
          </div>
          <nav className="hidden items-center gap-8 text-[13px] text-zinc-500 md:flex">
            <a href="#live" onClick={scrollToSection("#live")} className="transition hover:text-white">Live demo</a>
            <a href="#how" onClick={scrollToSection("#how")} className="transition hover:text-white">How it works</a>
            <a href="#loads" onClick={scrollToSection("#loads")} className="transition hover:text-white">Loads</a>
            <a href="#status" onClick={scrollToSection("#status")} className="transition hover:text-white">Status</a>
          </nav>
          {user ? (
            <button
              onClick={handleAccountClick}
              title={user.email ?? ""}
              className="flex items-center gap-2.5 rounded-full border border-white/15 py-1 pl-1 pr-4 transition hover:border-white/40"
            >
              <Avatar name={user.displayName} email={user.email} photoURL={user.photoURL} />
              <span className="max-w-28 truncate text-[13px] text-zinc-200">
                {user.displayName?.split(" ")[0] ?? "Account"}
              </span>
            </button>
          ) : (
            <button
              onClick={handleSignIn}
              disabled={signingIn}
              className="flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-full bg-white px-5 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-wait disabled:opacity-70"
            >
              {signingIn && <span className="spinner" />}
              {signingIn ? "Signing in…" : "Sign in"}
            </button>
          )}
        </header>
        {authError && (
          <p className="mx-auto max-w-7xl px-6 text-right font-mono text-[11px] text-orange-300 sm:px-10 lg:px-16">{authError}</p>
        )}

        {/* hero */}
        <section className="mx-auto max-w-7xl px-6 pt-12 text-center sm:px-16 sm:pt-20 lg:px-24">
          <p className="rise font-mono text-[12px] tracking-[0.08em] text-zinc-500" style={{ animationDelay: "0ms" }}>
            <span className="text-zinc-200">heliotrope</span> <span className="text-zinc-600">· n. —</span> <span className="italic">a plant that turns to face the sun</span>
          </p>
          <h1 className="rise mx-auto mt-8 max-w-5xl text-5xl font-semibold leading-[1.06] tracking-[-0.035em] sm:text-7xl" style={{ animationDelay: "120ms" }}>
            Heavy loads, run when
            <br />
            power is <span className="text-lime-300">cleanest.</span>
          </h1>
          <p className="rise mx-auto mt-8 max-w-xl text-[15px] leading-8 text-zinc-400 sm:text-base sm:leading-8" style={{ animationDelay: "240ms" }}>
            Named for the flower that leans toward the sun, Heliotrope leans your
            loads the same way — solving every 15 minutes for the schedule that
            minimizes carbon by favoring clean electricity. Always ready on time.
          </p>
          <div className="rise mt-12 flex flex-col items-stretch justify-center gap-4 sm:flex-row sm:items-center" style={{ animationDelay: "360ms" }}>
            <a href="#live" onClick={scrollToSection("#live")} className="rounded-full bg-white px-8 py-3.5 text-center text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98]">
              See it on today&apos;s grid
            </a>
            <a href="#how" onClick={scrollToSection("#how")} className="rounded-full border border-white/15 px-8 py-3.5 text-center text-sm text-zinc-300 transition hover:border-white/40 hover:text-white active:scale-[0.98]">
              How it works →
            </a>
          </div>
        </section>

        <LivePlan />

        {/* how */}
        <section id="how" className="mx-auto max-w-7xl px-6 py-24 sm:px-10 sm:py-32 lg:px-16">
          <div className="reveal js-reveal grid gap-14 md:grid-cols-[1fr_1.4fr] md:gap-24">
            <div>
              <h2 className="text-3xl font-semibold tracking-[-0.02em] sm:text-4xl">Set it.<br />Forget it.</h2>
              <p className="mt-4 text-[15px] leading-7 text-zinc-500">
                No new habits. No cold showers. You state the need, Heliotrope handles the when.
              </p>
            </div>
            <ol className="divide-y divide-white/10 border-y border-white/10">
              {[
                ["You say what's needed", "“Car charged by 7am. Hot water by 6. Laundry tonight.” That's all it asks."],
                ["It finds the clean window", "Power dips and peaks through the day. Heliotrope slides each load into a clean, cheap slot that still meets your time."],
                ["It's ready, with proof", "Every run shows why: what moved, what it saved, and the guarantee it never misses your time."],
              ].map(([t, d], i) => (
                <li key={t} className="flex gap-6 py-8">
                  <span className="font-mono text-[12px] text-zinc-600">0{i + 1}</span>
                  <div>
                    <h3 className="text-[15px] font-medium">{t}</h3>
                    <p className="mt-1.5 text-sm leading-6 text-zinc-500">{d}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* loads */}
        <section id="loads" className="border-y border-white/10 bg-[#080808]">
          <div className="mx-auto max-w-7xl px-4 py-20 sm:px-10 sm:py-24 lg:px-16">
            <h2 className="mx-auto max-w-xl text-center text-3xl font-semibold tracking-[-0.02em] sm:text-4xl">
              Made for the loads that matter
            </h2>
            <p className="mx-auto mt-4 max-w-md text-center text-[15px] leading-7 text-zinc-500">
              Each kind of appliance has its own physics. Heliotrope models the constraint, not just a start time.
            </p>
            <ul className="reveal js-reveal mx-auto mt-12 grid max-w-4xl gap-3 sm:grid-cols-2">
              {[
                {
                  title: "EV charging",
                  kind: "Interruptible",
                  desc: "Needs a total amount of energy by a deadline. It can pause and resume, so it fills the cleanest hours first.",
                  need: "needs: kW, kWh, ready-by time",
                },
                {
                  title: "Water heating",
                  kind: "Thermal",
                  desc: "Heat is stored. The solver heats ahead of time and keeps the water inside your comfort band.",
                  need: "needs: kW, comfort band, ready-by time",
                },
                {
                  title: "Laundry",
                  kind: "One continuous run",
                  desc: "Once a cycle starts it must finish. The solver picks the single cleanest start that meets your time.",
                  need: "needs: kW, cycle length, ready-by time",
                },
                {
                  title: "Pumps and motors",
                  kind: "Interruptible",
                  desc: "Fills a tank in pieces. Runs when the grid is cleanest and stops before the dirty evening peak.",
                  need: "needs: kW, kWh, ready-by time",
                },
              ].map((c) => (
                <li key={c.title} className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <h3 className="text-[15px] font-medium text-zinc-100">{c.title}</h3>
                    <span className="font-mono text-[11px] text-lime-300/90">{c.kind}</span>
                  </div>
                  <p className="mt-2 text-sm leading-6 text-zinc-500">{c.desc}</p>
                  <p className="mt-3 font-mono text-[11px] text-zinc-600">{c.need}</p>
                </li>
              ))}
            </ul>
            <p className="mx-auto mt-8 max-w-lg text-center text-[13px] leading-6 text-zinc-600">
              Several loads sharing one supply are scheduled together under a single capacity limit, so they never stack into a new peak.
            </p>
          </div>
        </section>

        {/* status: what is real today */}
        <section id="status" className="mx-auto max-w-7xl px-4 py-20 sm:px-10 sm:py-28 lg:px-16">
          <div className="reveal js-reveal mx-auto max-w-3xl">
            <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-zinc-600">Where it stands</p>
            <h2 className="mt-3 text-3xl font-semibold tracking-[-0.02em] sm:text-4xl">A working prototype, honestly labelled.</h2>
            <div className="mt-8 grid gap-3 sm:grid-cols-2">
              <div className="rounded-2xl border border-lime-300/20 bg-lime-300/[0.03] p-6">
                <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-lime-300/80">Works today</p>
                <ul className="mt-3 space-y-2.5 text-[13px] leading-6 text-zinc-300">
                  <li>Describe a load in plain words; an AI model or built-in rules classify it.</li>
                  <li>An exact solver (OR-Tools CP-SAT) places it with hard deadlines.</li>
                  <li>A live grid-carbon estimate and forecast from real solar and wind data.</li>
                  <li>Track, replan and compare against running everything now.</li>
                </ul>
              </div>
              <div className="rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
                <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">Not yet</p>
                <ul className="mt-3 space-y-2.5 text-[13px] leading-6 text-zinc-400">
                  <li>Smart-meter or device control: today you confirm when a load actually runs.</li>
                  <li>Metered grid data: the signal is an estimate from weather, and says so.</li>
                  <li>Field results: there is no pilot data yet, so no savings claim beyond the live solver above.</li>
                </ul>
              </div>
            </div>
          </div>

          <div className="mx-auto mt-20 max-w-xl text-center">
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Put your building on the clean part of the day.</h2>
            <p className="mt-3 text-sm leading-6 text-zinc-500">Start with one hostel, one floor, or one home. Ready times guaranteed.</p>
            <div className="mt-7 flex flex-col items-stretch justify-center gap-3 sm:flex-row sm:items-center">
              {user ? (
                <button onClick={handleAccountClick} className="cursor-pointer rounded-full bg-lime-300 px-8 py-3 text-sm font-medium text-black transition hover:bg-lime-200 active:scale-[0.98]">
                  Open your dashboard
                </button>
              ) : (
                <button onClick={handleSignIn} disabled={signingIn} className="cursor-pointer rounded-full bg-lime-300 px-8 py-3 text-sm font-medium text-black transition hover:bg-lime-200 active:scale-[0.98] disabled:cursor-wait disabled:opacity-70">
                  {signingIn ? "Signing in…" : "Try it with your loads"}
                </button>
              )}
              <a href="mailto:stellarieX@proton.me" className="rounded-full border border-white/15 px-8 py-3 text-center text-sm text-zinc-300 transition hover:border-white/40 hover:text-white">
                Talk to us
              </a>
            </div>
          </div>
        </section>

        <footer className="border-t border-white/10">
          <div className="mx-auto flex max-w-7xl flex-col items-center justify-between gap-2 px-6 py-8 text-[12px] text-zinc-600 sm:flex-row sm:px-10 lg:px-16">
            <span>Heliotrope</span>
            <a href="mailto:stellarieX@proton.me" className="inline-flex min-h-11 items-center transition hover:text-zinc-300">stellarieX@proton.me</a>
            <span>© {new Date().getFullYear()}</span>
          </div>
        </footer>
      </div>
    </main>
  );
}
