"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  compareSchedulers,
  getCarbonSignal,
  waitForBackend,
  type CompareSchedulerResult,
} from "../lib/api/client";
import type { CarbonSignalResponse, SignalType } from "../lib/api/types";
import { buildSpecs, nextSlot, type StoredJob } from "../lib/loads/specs";

// Example household loads for the live demo. These are plain inputs; every number
// shown below (windows, kg of CO2, % saved) is computed by the backend solver on
// the live grid signal, not drawn by hand.
const SAMPLE: Array<Omit<StoredJob, "flexHours">> = [
  { id: "ev", name: "EV charger", kind: "EV charging", shiftable: true, powerKw: 7.4, readyBy: "07:00", jobType: "DEFERRABLE_INTERRUPTIBLE", energyKwh: 20 },
  { id: "geyser", name: "Water heater", kind: "Water heating", shiftable: true, powerKw: 2, readyBy: "06:00", jobType: "THERMAL" },
  { id: "washer", name: "Washing machine", kind: "Laundry", shiftable: true, powerKw: 2, readyBy: "21:00", jobType: "DEFERRABLE_ATOMIC", durationMin: 60 },
  { id: "pump", name: "Borewell pump", kind: "Pumping", shiftable: true, powerKw: 1.5, readyBy: "08:00", jobType: "DEFERRABLE_INTERRUPTIBLE", energyKwh: 4 },
];

const DAY_MS = 24 * 3600 * 1000;
const SLOT_MS = 15 * 60 * 1000;

type Phase = "connecting" | "waking" | "ready" | "down";

/** Merge consecutive 15-minute allocations into [startMs, endMs) windows. */
function windowsOf(allocs: Array<{ slot: number; timestamp: string }>): Array<[number, number]> {
  const sorted = [...allocs].sort((a, b) => a.slot - b.slot);
  const out: Array<[number, number]> = [];
  let i = 0;
  while (i < sorted.length) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1].slot === sorted[j].slot + 1) j++;
    out.push([new Date(sorted[i].timestamp).getTime(), new Date(sorted[j].timestamp).getTime() + SLOT_MS]);
    i = j + 1;
  }
  return out;
}

/** Plain-words description of the grid signal the backend actually used. */
function describeSignal(type: string | undefined, source?: string): { line: string; noun: string } {
  switch (type) {
    case "PROXY":
      return {
        line: "grid signal: an estimate built from solar and wind forecasts, not metered grid data",
        noun: "a weather-estimated signal",
      };
    case "SYNTHETIC":
      return {
        line: "grid signal: made-up test data from this server, not the real grid",
        noun: "test data",
      };
    case "MARGINAL":
    case "AVERAGE":
      return {
        line: `grid signal: ${type.toLowerCase()} grid data${source ? ` from ${source}` : ""}`,
        noun: `${type.toLowerCase()} grid data`,
      };
    case "FORECAST":
      return { line: `grid signal: a forecast${source ? ` from ${source}` : ""}`, noun: "a forecast signal" };
    default:
      return { line: "grid signal: loading…", noun: "the grid signal" };
  }
}

const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

export default function LivePlan({ onSignalType }: { onSignalType?: (type: SignalType) => void } = {}) {
  const [phase, setPhase] = useState<Phase>("connecting");
  const [attempt, setAttempt] = useState(0);
  const [flex, setFlex] = useState(2);
  const [withHeliotrope, setWithHeliotrope] = useState(true);
  const [signal, setSignal] = useState<CarbonSignalResponse | null>(null);
  const [plan, setPlan] = useState<{ asap: CompareSchedulerResult; opt: CompareSchedulerResult; flex: number; t0: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The solver may be asleep on free hosting: wait for it, then load the real signal.
  useEffect(() => {
    const ctl = new AbortController();
    void waitForBackend({ signal: ctl.signal, onWaking: () => setPhase("waking") }).then((up) => {
      if (ctl.signal.aborted) return;
      if (!up) {
        setPhase("down");
        return;
      }
      setPhase("ready");
      const start = nextSlot();
      getCarbonSignal({ start: start.toISOString(), end: new Date(start.getTime() + DAY_MS).toISOString() })
        .then((s) => {
          if (ctl.signal.aborted) return;
          setSignal(s);
          onSignalType?.(s.signal_type);
        })
        .catch(() => {
          if (!ctl.signal.aborted) setError("Couldn't read the grid signal.");
        });
    });
    return () => ctl.abort();
    // onSignalType is a parent state setter; only a retry should refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt]);

  const solveSeq = useRef(0);
  const solve = useCallback(async (flexHours: number) => {
    const seq = ++solveSeq.current;
    const t0 = nextSlot();
    const jobs: StoredJob[] = SAMPLE.map((j) => ({ ...j, flexHours }));
    const { specs } = buildSpecs(jobs, t0);
    setBusy(true);
    setError(null);
    try {
      const cmp = await compareSchedulers({ jobs: specs, capacity_kw: 15, scheduler: "CPSAT", schedulers: ["ASAP", "CPSAT"] });
      const asap = cmp.results.ASAP;
      const opt = cmp.results.CPSAT;
      if (!asap || !opt) throw new Error("incomplete answer");
      if (opt.status !== "OPTIMAL" && opt.status !== "FEASIBLE") throw new Error("no feasible plan");
      if (seq !== solveSeq.current) return; // a newer slider position superseded this answer
      setPlan({ asap, opt, flex: flexHours, t0: t0.getTime() });
    } catch {
      if (seq === solveSeq.current) setError("The solver didn't give a usable plan. Try again in a moment.");
    } finally {
      if (seq === solveSeq.current) setBusy(false);
    }
  }, []);

  // Re-solve (debounced) whenever the flexibility slider moves.
  useEffect(() => {
    if (phase !== "ready") return;
    const t = setTimeout(() => void solve(flex), 350);
    return () => clearTimeout(t);
  }, [phase, flex, solve]);

  const view = withHeliotrope ? plan?.opt : plan?.asap;
  const t0 = plan?.t0 ?? 0;

  // The real carbon curve as an SVG path over the same 24h axis as the bars.
  const curve = useMemo(() => {
    if (!signal || signal.points.length < 2) return null;
    const vals = signal.points.map((p) => p.carbon_intensity_gco2_per_kwh);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const W = 800;
    const H = 150;
    const x = (i: number) => (i / (vals.length - 1)) * W;
    const y = (v: number) => H - 8 - ((v - lo) / Math.max(hi - lo, 1)) * (H - 24);
    const d = vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    return { d, W, H, lo, hi };
  }, [signal]);

  // Describe the signal the solver really used for the numbers, else the one fetched for the curve.
  const sig = describeSignal(plan?.opt.signal?.signal_type ?? signal?.signal_type, signal?.source);
  const saved = plan?.opt.metrics.co2_saved_percent ?? null;
  const savedKg = plan?.opt.metrics.co2_saved_kg ?? null;
  const ticks = [0, 6, 12, 18, 24];

  return (
    <section id="live" className="mx-auto max-w-7xl px-4 pt-16 sm:px-10 sm:pt-24 lg:px-16">
      <div className="reveal js-reveal overflow-hidden rounded-3xl border border-white/10 bg-[#0a0a0a]">
        <div className="flex flex-wrap items-center justify-between gap-4 px-5 py-5 sm:px-10">
          <div>
            <p className="flex items-center gap-2 text-[13px] text-zinc-400">
              <span className={`h-1.5 w-1.5 rounded-full ${phase === "ready" ? "live-dot bg-lime-300" : phase === "down" ? "bg-red-400" : "animate-pulse bg-amber-400"}`} />
              {phase === "ready" ? "Computed live by the solver on the next 24 hours of grid-carbon data" : phase === "down" ? "The solver isn't answering" : phase === "waking" ? "Waking the solver…" : "Connecting to the solver…"}
            </p>
            {signal && (
              <p className="mt-1 font-mono text-[11px] text-zinc-600">{describeSignal(signal.signal_type, signal.source).line}</p>
            )}
          </div>
          <div className="flex rounded-full border border-white/10 p-1 text-[12px]">
            <button
              onClick={() => setWithHeliotrope(false)}
              className={`min-h-10 cursor-pointer rounded-full px-4 py-1.5 transition active:scale-[0.96] ${!withHeliotrope ? "bg-white font-medium text-black" : "text-zinc-500 hover:text-white"}`}
            >
              Run everything now
            </button>
            <button
              onClick={() => setWithHeliotrope(true)}
              className={`min-h-10 cursor-pointer rounded-full px-4 py-1.5 transition active:scale-[0.96] ${withHeliotrope ? "bg-white font-medium text-black" : "text-zinc-500 hover:text-white"}`}
            >
              Scheduled by Heliotrope
            </button>
          </div>
        </div>

        {phase !== "ready" || !plan ? (
          <div className="px-5 pb-10 pt-4 sm:px-10">
            <div className="h-36 animate-pulse rounded-2xl bg-white/[0.04] sm:h-44" />
            <p className="mt-4 text-[13px] leading-6 text-zinc-500">
              {phase === "down" ? (
                <>
                  The compute backend isn&apos;t reachable right now.{" "}
                  <button onClick={() => { setPhase("connecting"); setAttempt((n) => n + 1); }} className="cursor-pointer text-zinc-300 underline underline-offset-4 hover:text-white">
                    Try again
                  </button>
                </>
              ) : error ? (
                error
              ) : phase === "waking" ? (
                "The solver server was idle and is waking up, which can take up to a minute. The real numbers appear here as soon as it answers."
              ) : (
                "Connecting to the solver…"
              )}
            </p>
          </div>
        ) : (
          <>
            <div className="px-5 sm:px-10">
              {curve && (
                <>
                  <svg viewBox={`0 0 ${curve.W} ${curve.H}`} className="h-32 w-full sm:h-44" preserveAspectRatio="none" role="img" aria-label={`Grid carbon intensity over the next 24 hours, from ${sig.noun}`}>
                    <defs>
                      <linearGradient id="co2fill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#fff" stopOpacity="0.16" />
                        <stop offset="100%" stopColor="#fff" stopOpacity="0" />
                      </linearGradient>
                    </defs>
                    {view?.schedule.flatMap((sj) => windowsOf(sj.allocations)).map(([a, b], i) => (
                      <rect
                        key={i}
                        x={((a - t0) / DAY_MS) * curve.W}
                        width={Math.max(((b - a) / DAY_MS) * curve.W, 2)}
                        y="0"
                        height={curve.H}
                        fill={withHeliotrope ? "#a3e635" : "#fb923c"}
                        opacity="0.07"
                      />
                    ))}
                    <path d={`${curve.d} L${curve.W},${curve.H} L0,${curve.H} Z`} fill="url(#co2fill)" />
                    <path d={curve.d} fill="none" stroke={withHeliotrope ? "#a3e635" : "#fb923c"} strokeWidth="2" vectorEffect="non-scaling-stroke" />
                  </svg>
                  <div className="flex flex-wrap justify-between gap-x-4 gap-y-1 pb-2 font-mono text-[10px] text-zinc-600">
                    <span>Cleanest hour: {Math.round(curve.lo)} grams of CO₂ per kWh of electricity</span>
                    <span>Dirtiest: {Math.round(curve.hi)}</span>
                  </div>
                </>
              )}
            </div>

            <div className="px-5 pb-3 sm:px-10">
              <div className="flex justify-between border-t border-white/5 pt-3 font-mono text-[10px] text-zinc-600 sm:pl-[8.5rem] sm:pr-[12.5rem]">
                {ticks.map((h) => (
                  <span key={h}>{clock(t0 + h * 3600 * 1000)}</span>
                ))}
              </div>
              {SAMPLE.map((j) => {
                const sj = view?.schedule.find((x) => x.job_id === j.id);
                const wins = sj ? windowsOf(sj.allocations) : [];
                const ex = plan.opt.explanations.find((e) => e.job_id === j.id);
                return (
                  <div key={j.id} className="flex flex-col gap-2 border-t border-white/5 py-3.5 sm:flex-row sm:items-center sm:gap-5">
                    <div className="shrink-0 sm:w-32">
                      <div className="text-[13px] text-zinc-200">{j.name}</div>
                      <div className="text-[11px] text-zinc-600">needed by {j.readyBy}</div>
                    </div>
                    <div className="relative h-7 flex-1 rounded-md bg-white/[0.04]">
                      {wins.map(([a, b], i) => (
                        <div
                          key={i}
                          title={`${clock(a)}–${clock(b)}`}
                          className={`absolute bottom-1 top-1 rounded-sm transition-all duration-500 ${withHeliotrope ? "bg-lime-300" : "bg-zinc-500"}`}
                          style={{ left: `${Math.max(0, ((a - t0) / DAY_MS) * 100)}%`, width: `${Math.max(((b - a) / DAY_MS) * 100, 0.6)}%` }}
                        />
                      ))}
                    </div>
                    <div className="font-mono text-[11px] text-zinc-500 sm:w-48 sm:text-right">
                      {wins.length === 0
                        ? "—"
                        : withHeliotrope
                          ? ex && ex.co2_saved_kg !== null && ex.co2_saved_kg > 0.005
                            ? `starts ${clock(wins[0][0])} · saves ${ex.co2_saved_kg.toFixed(2)} kg CO₂`
                            : `starts ${clock(wins[0][0])} · already at the cleanest time`
                          : `starts ${clock(wins[0][0])} · runs right away`}
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="flex flex-col gap-4 border-t border-white/10 bg-white/[0.015] px-5 py-5 sm:flex-row sm:items-center sm:justify-between sm:px-10 sm:py-6">
              <label className="flex flex-col gap-2 text-[13px] text-zinc-400 sm:flex-row sm:items-center sm:gap-3">
                <span>
                  Each load can finish up to <span className="font-mono text-white">{flex} h</span> late
                </span>
                <input type="range" min={0} max={6} value={flex} onChange={(e) => setFlex(Number(e.target.value))} className="h-8 w-full cursor-pointer accent-lime-300 sm:w-40" />
              </label>
              <p className="font-mono text-[12px] text-zinc-500" aria-live="polite">
                {busy ? (
                  "solving…"
                ) : saved !== null && saved > 0.05 ? (
                  <>
                    <span className="text-lg text-lime-300">{saved.toFixed(0)}% less CO₂</span> than running everything now
                    {savedKg !== null ? ` (${savedKg.toFixed(1)} kg)` : ""}, using {sig.noun}
                  </>
                ) : (
                  "No cleaner window fits these deadlines, so the schedule matches running everything now."
                )}
              </p>
            </div>
          </>
        )}
        <p className="border-t border-white/5 px-5 py-3 text-[11px] leading-5 text-zinc-600 sm:px-10">
          Example loads (not yours) with Max power at once set to 15 kW. Every &ldquo;needed by&rdquo; time is a hard limit; the solver only minimises carbon inside it. Sign in to plan your own.
        </p>
      </div>
    </section>
  );
}
