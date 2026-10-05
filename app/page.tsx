"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { onAuthStateChanged, signInWithPopup, type User } from "firebase/auth";
import { getFirebaseAuth, getGoogleProvider } from "../lib/firebase";

function useInView<T extends HTMLElement>(threshold = 0.2) {
  const ref = useRef<T | null>(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) {
          setInView(true);
          io.disconnect();
        }
      },
      { threshold }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [threshold]);
  return { ref, inView };
}

const JOBS = [
  { name: "EV", detail: "ready by 7am", asap: [18.5, 22.5], opt: [11, 15], why: "charged at midday · −1.9 kg" },
  { name: "Geyser", detail: "hot by 6am", asap: [19, 21], opt: [12, 14], why: "heated at midday · −0.8 kg" },
  { name: "Laundry", detail: "done by 9pm", asap: [19, 20.5], opt: [12.5, 14], why: "ran at midday · −0.6 kg" },
  { name: "Bedroom AC", detail: "cool by 10pm", asap: [20, 23], opt: [13, 16], why: "pre-cooled · −0.4 kg" },
];

function WeekBars() {
  const { ref, inView } = useInView<HTMLDivElement>(0.4);
  return (
    <div ref={ref} className="mt-8 space-y-4">
      {(
        [
          ["Week 1 · as usual", "214 kg CO₂", 100, "text-zinc-500", "bg-zinc-700"],
          ["Week 2 · with Heliotrope", "126 kg CO₂ · −41%", 59, "text-zinc-100", "bg-lime-300"],
        ] as const
      ).map(([label, value, w, tc, bc]) => (
        <div key={label}>
          <div className="mb-1.5 flex items-baseline justify-between text-[13px]">
            <span className="text-zinc-400">{label}</span>
            <span className={`font-mono ${tc}`}>{value}</span>
          </div>
          <div className="h-2.5 overflow-hidden rounded-full bg-white/[0.06]">
            <div
              className={`h-full rounded-full ${bc} transition-[width] duration-[1.2s] ease-[cubic-bezier(0.22,1,0.36,1)]`}
              style={{ width: inView ? `${w}%` : "0%" }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

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

function curvePath(values: number[], w: number, h: number, max: number, min: number) {
  const px = (i: number) => (i / (values.length - 1)) * w;
  const py = (v: number) => h - ((v - min) / (max - min)) * (h - 8) - 4;
  return values.map((v, i) => `${i === 0 ? "M" : "L"}${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
}

export default function Home() {
  const router = useRouter();
  const [auth] = useState(() => getFirebaseAuth());
  const [withHeliotrope, setWithHeliotrope] = useState(true);
  const [flex, setFlex] = useState(2);
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

  const hours = useMemo(() => Array.from({ length: 97 }, (_, i) => (i * 24) / 96), []);
  // Without: everyone runs ~19–22h → sharp evening peak. With: spread midday → near-flat.
  const loadWithout = useMemo(
    () =>
      hours.map(
        (h) =>
          10 +
          34 * Math.exp(-((h - 20) ** 2) / 2.6) +
          8 * Math.exp(-((h - 8) ** 2) / 5) +
          2.2 * Math.sin(h * 1.7)
      ),
    [hours]
  );
  const loadWith = useMemo(
    () =>
      hours.map(
        (h) =>
          15.5 +
          5.5 * Math.exp(-((h - 13) ** 2) / 9) +
          2.5 * Math.exp(-((h - 8) ** 2) / 8) +
          1.1 * Math.sin(h * 1.7)
      ),
    [hours]
  );
  const max = Math.max(...loadWithout) * 1.06;
  const min = 6;
  const W = 800;
  const H = 190;
  const lineWithout = useMemo(() => curvePath(loadWithout, W, H, max, min), [loadWithout, max]);
  const lineWith = useMemo(() => curvePath(loadWith, W, H, max, min), [loadWith, max]);

  const saved = Math.min(52, (withHeliotrope ? 31 : 0) + flex * 4.5);

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
            <a href="#how" onClick={scrollToSection("#how")} className="transition hover:text-white">How it works</a>
            <a href="#loads" onClick={scrollToSection("#loads")} className="transition hover:text-white">Loads</a>
            <a href="#week" onClick={scrollToSection("#week")} className="transition hover:text-white">A week</a>
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
              className="flex cursor-pointer items-center justify-center gap-2 rounded-full bg-white px-5 py-2 text-[13px] font-medium text-black transition hover:bg-zinc-200 active:scale-[0.97] disabled:cursor-wait disabled:opacity-70"
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
        <section className="mx-auto max-w-7xl px-10 pt-12 text-center sm:px-16 sm:pt-20 lg:px-24">
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
            <a href="#simulator" onClick={scrollToSection("#simulator")} className="rounded-full bg-white px-8 py-3.5 text-center text-sm font-medium text-black transition hover:bg-zinc-200 active:scale-[0.98]">
              Try it with your day
            </a>
            <a href="#week" onClick={scrollToSection("#week")} className="rounded-full border border-white/15 px-8 py-3.5 text-center text-sm text-zinc-300 transition hover:border-white/40 hover:text-white active:scale-[0.98]">
              A hostel week →
            </a>
          </div>
        </section>

        {/* simulator */}
        <section id="simulator" className="mx-auto max-w-7xl px-6 pt-16 sm:px-10 sm:pt-24 lg:px-16">
          <div className="reveal js-reveal overflow-hidden rounded-3xl border border-white/10 bg-[#0a0a0a]">
            <div className="flex flex-wrap items-center justify-between gap-4 px-8 py-5 sm:px-10">
              <p className="flex items-center gap-2 text-[13px] text-zinc-500">
                <span className={`live-dot h-1.5 w-1.5 rounded-full ${withHeliotrope ? "bg-lime-300" : "bg-orange-400"}`} />
                {withHeliotrope ? (
                  <>Midday power, evening comfort. <span className="text-zinc-300">Everything ready on time.</span></>
                ) : (
                  <>Plug in after work. <span className="text-zinc-300">Everything runs at the dirtiest hour.</span></>
                )}
              </p>
              <div className="flex rounded-full border border-white/10 p-1 text-[12px]">
                <button
                  onClick={() => setWithHeliotrope(false)}
                  className={`cursor-pointer rounded-full px-4 py-1.5 transition active:scale-[0.96] ${!withHeliotrope ? "bg-white font-medium text-black" : "text-zinc-500 hover:text-white"}`}
                >
                  Without
                </button>
                <button
                  onClick={() => setWithHeliotrope(true)}
                  className={`cursor-pointer rounded-full px-4 py-1.5 transition active:scale-[0.96] ${withHeliotrope ? "bg-white font-medium text-black" : "text-zinc-500 hover:text-white"}`}
                >
                  With Heliotrope
                </button>
              </div>
            </div>

            <div className="px-8 sm:px-10">
              <svg viewBox={`0 0 ${W} ${H}`} className="h-40 w-full sm:h-48" preserveAspectRatio="none">
                <defs>
                  <linearGradient id="loadfill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#fff" stopOpacity="0.2" />
                    <stop offset="100%" stopColor="#fff" stopOpacity="0" />
                  </linearGradient>
                </defs>
                <rect x={W * 0.42} y="0" width={W * 0.2} height={H} fill="#34d399" opacity="0.06" />
                <rect x={W * 0.72} y="0" width={W * 0.18} height={H} fill="#fb923c" opacity="0.07" />
                {/* without: sharp evening peak */}
                <g className={`transition-opacity duration-700 ${withHeliotrope ? "opacity-0" : "opacity-100"}`}>
                  <path d={`${lineWithout} L${W},${H} L0,${H} Z`} fill="url(#loadfill)" />
                  <path d={lineWithout} fill="none" stroke="#fb923c" strokeWidth="2" opacity="0.95" />
                </g>
                {/* with: spread flat */}
                <g className={`transition-opacity duration-700 ${withHeliotrope ? "opacity-100" : "opacity-0"}`}>
                  <path d={`${lineWith} L${W},${H} L0,${H} Z`} fill="url(#loadfill)" />
                  <path d={lineWith} fill="none" stroke="#a3e635" strokeWidth="2" opacity="0.95" />
                </g>
              </svg>
              <div className="flex justify-between pb-2 font-mono text-[10px] text-zinc-600">
                <span>morning</span><span>midday · spread here</span><span>evening · {withHeliotrope ? "rested" : "peak 48 kW"}</span>
              </div>
            </div>

            <div className="space-y-1 px-8 pb-3 sm:px-10">
              {JOBS.map((j) => {
                const [s, e] = withHeliotrope ? j.opt : j.asap;
                return (
                  <div key={j.name} className="flex items-center gap-5 border-t border-white/5 py-4">
                    <div className="w-32 shrink-0">
                      <div className="text-[13px] text-zinc-200">{j.name}</div>
                      <div className="text-[11px] text-zinc-600">{j.detail}</div>
                    </div>
                    <div className="relative h-7 flex-1 rounded-md bg-white/[0.04]">
                      <div
                        className={`absolute top-1 bottom-1 rounded-md transition-all duration-700 ${withHeliotrope ? "bg-lime-300" : "bg-zinc-600"}`}
                        style={{ left: `${(s / 24) * 100}%`, width: `${((e - s) / 24) * 100}%` }}
                      />
                    </div>
                    <div className="hidden w-48 shrink-0 text-right font-mono text-[11px] text-zinc-500 sm:block">{j.why}</div>
                  </div>
                );
              })}
            </div>

            <div className="flex flex-col gap-4 border-t border-white/10 bg-white/[0.015] px-8 py-6 sm:flex-row sm:items-center sm:justify-between sm:px-10">
              <label className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[13px] text-zinc-400">
                <span>I can be flexible by <span className="font-mono text-white">+{flex}h</span></span>
                <input type="range" min={0} max={6} value={flex} onChange={(e) => setFlex(Number(e.target.value))} className="w-36 cursor-pointer accent-lime-300" />
              </label>
              <p className="font-mono text-[12px] text-zinc-500">
                your footprint drops <span className="text-lg text-lime-300">−{saved.toFixed(0)}%</span>
              </p>
            </div>
          </div>
        </section>

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
          <div className="mx-auto max-w-7xl px-6 py-24 sm:px-10 lg:px-16">
            <h2 className="mx-auto max-w-xl text-center text-3xl font-semibold tracking-[-0.02em] sm:text-4xl">
              Made for the loads that matter
            </h2>
            <p className="mx-auto mt-4 max-w-md text-center text-[15px] leading-7 text-zinc-500">
              One clean window, four loads lined up through the day.
            </p>
            <div className="reveal js-reveal relative mx-auto mt-14 max-w-2xl">
              <div aria-hidden className="absolute bottom-8 left-[27px] top-8 w-px bg-white/10 sm:left-[31px]" />
              <ol className="space-y-2">
                {[
                  {
                    time: "12:00",
                    title: "Geyser heats",
                    desc: "Heats once at midday, stays hot for the morning. Skips the evening rush entirely.",
                    kw: "3.0 kW · ready by 6am",
                    icon: (
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
                        <rect x="7" y="2.5" width="10" height="16" rx="2.5" />
                        <path d="M12 6.5c-1.6 2-2.6 3.2-2.6 4.6a2.6 2.6 0 0 0 5.2 0c0-1.4-1-2.6-2.6-4.6Z" />
                        <path d="M10 21h4" />
                      </svg>
                    ),
                  },
                  {
                    time: "12:40",
                    title: "Laundry runs",
                    desc: "The most movable hour in the house. Same fresh clothes, a third of the footprint.",
                    kw: "2.1 kW · done by 9pm",
                    icon: (
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
                        <rect x="4" y="2.5" width="16" height="19" rx="2.5" />
                        <circle cx="12" cy="13" r="4.5" />
                        <path d="M9.8 13a2.2 2.2 0 0 0 4.4 0" />
                        <path d="M7.5 5.5h.01M10 5.5h4" />
                      </svg>
                    ),
                  },
                  {
                    time: "13:10",
                    title: "EV charges",
                    desc: "Midday top-up while the grid breathes easy. Full by morning, no evening spike.",
                    kw: "7.2 kW · ready by 7am",
                    icon: (
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
                        <path d="M4 16v-4.5L6.5 7h8L17 11.5H19a1.5 1.5 0 0 1 1.5 1.5v3H19" />
                        <path d="M4 16h2.5M10.5 16h5" />
                        <circle cx="8" cy="16.5" r="1.8" />
                        <circle cx="16" cy="16.5" r="1.8" />
                        <path d="M13 8.5 12 11h2l-1 2.5" />
                      </svg>
                    ),
                  },
                  {
                    time: "15:30",
                    title: "Rooms pre-cool",
                    desc: "Cools ahead of the peak so the compressor rests through the dirtiest hours.",
                    kw: "1.4 kW · cool by 10pm",
                    icon: (
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="h-5 w-5">
                        <rect x="3" y="4" width="18" height="6" rx="1.5" />
                        <path d="M7 14c0 1.5 2 1.5 2 3s-2 1.5-2 3M12 14c0 1.5 2 1.5 2 3s-2 1.5-2 3M17 14c0 1.5 2 1.5 2 3s-2 1.5-2 3" />
                        <path d="M6.5 7h.01M18 7h.01" />
                      </svg>
                    ),
                  },
                ].map((s) => (
                  <li key={s.title} className="relative flex gap-5 rounded-2xl p-3 transition hover:bg-white/[0.025] sm:gap-6 sm:p-4">
                    <div className="relative z-10 grid h-14 w-14 shrink-0 place-items-center rounded-full border border-white/12 bg-black text-zinc-200 sm:h-16 sm:w-16">
                      {s.icon}
                    </div>
                    <div className="min-w-0 flex-1 pb-6">
                      <div className="flex flex-wrap items-baseline gap-x-3">
                        <span className="font-mono text-[11px] text-lime-300/90">{s.time}</span>
                        <h3 className="text-[15px] font-medium text-zinc-100">{s.title}</h3>
                      </div>
                      <p className="mt-1.5 max-w-md text-sm leading-6 text-zinc-500">{s.desc}</p>
                      <p className="mt-1.5 font-mono text-[11px] text-zinc-600">{s.kw}</p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
            <p className="mx-auto mt-8 max-w-lg text-center text-[13px] leading-6 text-zinc-600">
              In shared buildings, Heliotrope also staggers everyone automatically — so twenty geysers don&apos;t all fire at once and create a new peak.
            </p>
          </div>
        </section>

        {/* pilot */}
        <section id="week" className="mx-auto max-w-7xl px-6 py-24 sm:px-10 sm:py-32 lg:px-16">
          <div className="reveal js-reveal grid gap-12 md:grid-cols-2 md:gap-16">
            <div>
              <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-zinc-600">The pilot · Block C, 40 rooms</p>
              <h2 className="mt-3 text-3xl font-semibold tracking-[-0.02em] sm:text-4xl">
                Two weeks. One meter. Real readings.
              </h2>
              <p className="mt-4 max-w-md text-[15px] leading-7 text-zinc-500">
                Week 1 we only watch: 2 geysers, 2 washers, 1 EV point, metered every
                15 minutes. Week 2 Heliotrope schedules the same needs. Same hostel,
                same habits — the difference is the when.
              </p>
              <WeekBars />
              <p className="mt-4 font-mono text-[11px] leading-5 text-zinc-600">model projection from Block C load totals — replaced by meter readings once the pilot runs.</p>
            </div>
            <div className="content-start">
              <div className="grid gap-px overflow-hidden rounded-2xl border border-white/10 bg-white/10">
                {[
                  ["−41%", "less carbon, same hot water"],
                  ["−44%", "lower evening peak"],
                  ["100%", "ready on time, every time"],
                  ["−22%", "lower energy bill *"],
                ].map(([v, l]) => (
                  <div key={l} className="flex items-baseline justify-between bg-black px-6 py-5">
                    <span className="font-mono text-2xl tracking-tight text-white">{v}</span>
                    <span className="text-right text-[13px] text-zinc-500">{l}</span>
                  </div>
                ))}
              </div>
              <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-6">
                <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">How a student team meters this</p>
                <ul className="mt-3 space-y-2.5 text-[13px] leading-6 text-zinc-400">
                  <li><span className="text-zinc-200">Loads</span> — clamp + plug meters on each geyser, washer, EV point; 15-min hostel register.</li>
                  <li><span className="text-zinc-200">Grid</span> — marginal intensity from Electricity Maps, or net-load proxy where unavailable.</li>
                  <li><span className="text-zinc-200">Bill</span> — hostel&apos;s actual day/night tariff applied to both weeks, line by line.</li>
                </ul>
              </div>
            </div>
          </div>
          <p className="mt-6 font-mono text-[11px] text-zinc-700">* bill estimate under a typical day/night tariff. Your rate may differ.</p>

          <div className="mx-auto mt-20 max-w-xl text-center">
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Put your building on the clean part of the day.</h2>
            <p className="mt-3 text-sm leading-6 text-zinc-500">Start with one hostel, one floor, or one home. Ready times guaranteed.</p>
            <a href="mailto:stellarieX@proton.me" className="mt-7 inline-block rounded-full bg-white px-8 py-3 text-sm font-medium text-black transition hover:bg-zinc-200">
              Talk to us
            </a>
          </div>
        </section>

        <footer className="border-t border-white/10">
          <div className="mx-auto flex max-w-7xl flex-col items-center justify-between gap-2 px-6 py-8 text-[12px] text-zinc-600 sm:flex-row sm:px-10 lg:px-16">
            <span>Heliotrope</span>
            <a href="mailto:stellarieX@proton.me" className="transition hover:text-zinc-300">stellarieX@proton.me</a>
            <span>© {new Date().getFullYear()}</span>
          </div>
        </footer>
      </div>
    </main>
  );
}
