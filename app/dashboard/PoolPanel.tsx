"use client";

import { useEffect, useMemo, useState } from "react";
import { getPoolStats } from "../../lib/api/client";
import type { PoolStats } from "../../lib/api/types";

const REFRESH_MS = 60_000;

/** "7:15 PM", or "Tue 1:00 AM" when it is not today. */
function clock(iso: string | null | undefined) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? t : `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-white/10 px-4 py-3">
      <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-zinc-600">{label}</p>
      <p className="mt-1 truncate text-xl font-semibold tracking-tight">{value}</p>
      {note && <p className="mt-0.5 truncate font-mono text-[11px] text-zinc-600">{note}</p>}
    </div>
  );
}

/** Small area chart of the combined planned load, in the style of the other charts. */
function PoolChart({ stats }: { stats: PoolStats }) {
  const W = 600;
  const H = 110;
  const slots = stats.slots;
  const geo = useMemo(() => {
    if (slots.length === 0) return null;
    const maxY = Math.max(0.1, ...slots.map((s) => s.kw));
    const x = (i: number) => (i / Math.max(slots.length - 1, 1)) * W;
    const y = (v: number) => H - (v / maxY) * (H - 12) - 4;
    const line = slots.map((s, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(s.kw).toFixed(1)}`).join(" ");
    let peakIdx = 0;
    slots.forEach((s, i) => {
      if (s.kw > slots[peakIdx].kw) peakIdx = i;
    });
    return { line, avgY: y(stats.average_kw), peakX: x(peakIdx), hasLoad: slots.some((s) => s.kw > 0) };
  }, [slots, stats.average_kw]);

  if (!geo) return null;
  return (
    <div className="mt-4">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-28 w-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Combined planned power of all active schedules, highest ${stats.peak_kw.toFixed(1)} kilowatts at ${clock(stats.peak_at)}`}
      >
        <path d={`${geo.line} L${W},${H} L0,${H} Z`} fill="#38bdf8" opacity="0.1" />
        <path d={geo.line} fill="none" stroke="#38bdf8" strokeWidth="1.8" vectorEffect="non-scaling-stroke" />
        {geo.hasLoad && (
          <>
            <line x1="0" x2={W} y1={geo.avgY} y2={geo.avgY} stroke="#fff" strokeWidth="1" strokeDasharray="5 4" opacity="0.4" vectorEffect="non-scaling-stroke" />
            <line x1={geo.peakX} x2={geo.peakX} y1="0" y2={H} stroke="#fbbf24" strokeWidth="1" strokeDasharray="2 3" opacity="0.7" vectorEffect="non-scaling-stroke" />
          </>
        )}
      </svg>
      <div className="mt-1 flex flex-wrap justify-between gap-x-3 gap-y-0.5 font-mono text-[10px] text-zinc-600">
        <span>{clock(stats.start)}</span>
        <span className="order-last w-full text-center sm:order-none sm:w-auto">
          {geo.hasLoad ? "dashed white line = average · amber line = busiest moment" : "nothing planned in this window"}
        </span>
        <span>{clock(stats.end)}</span>
      </div>
    </div>
  );
}

/** Everyone's planned load: the aggregate of all active schedules on the server, plus a plain
 *  explanation of why Heliotrope spreads new schedules out. Hidden quietly if the server
 *  doesn't offer the numbers (older deployment, or a hiccup). */
export default function PoolPanel({ enabled, refreshKey }: { enabled: boolean; refreshKey: number }) {
  const [stats, setStats] = useState<PoolStats | null>(null);
  const [failure, setFailure] = useState<"missing" | "error" | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const load = async () => {
      try {
        const s = await getPoolStats();
        if (cancelled) return;
        setStats(s);
        setFailure(null);
      } catch (e) {
        if (cancelled) return;
        setFailure((e as { status?: number }).status === 404 ? "missing" : "error");
      }
    };
    void load();
    // Refresh once a minute (skipped while the tab is hidden) and whenever the user schedules.
    const t = setInterval(() => {
      if (!document.hidden) void load();
    }, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [enabled, refreshKey]);

  if (!enabled) return null;

  if (!stats) {
    return (
      <p className="mt-3 px-1 font-mono text-[11px] text-zinc-700" role="status">
        {failure === "missing"
          ? "Everyone's planned load isn't available on this server yet."
          : failure === "error"
            ? "Couldn't load everyone's planned load right now. It will retry in a minute."
            : "Loading everyone's planned load…"}
      </p>
    );
  }

  const empty = stats.active_schedules === 0;
  return (
    <div className="mt-3 rounded-2xl border border-white/10 bg-[#0a0a0a] p-4 sm:p-8">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div>
          <h2 className="text-[15px] font-medium">Everyone&apos;s planned load</h2>
          <p className="mt-0.5 font-mono text-[11px] text-zinc-600">
            all active schedules on this server · updated {clock(stats.generated_at)}
          </p>
        </div>
        {failure && (
          <p className="font-mono text-[11px] text-amber-400/80" role="status">
            Couldn&apos;t refresh just now; showing the last numbers.
          </p>
        )}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Tile label="Active schedules" value={String(stats.active_schedules)} />
        <Tile label="Loads planned" value={String(stats.active_loads)} />
        <Tile label="Planned energy" value={`${stats.total_planned_kwh.toFixed(1)} kWh`} note="electricity they will use" />
        <Tile
          label="Busiest moment"
          value={empty ? "—" : `${stats.peak_kw.toFixed(1)} kW`}
          note={empty ? "nothing planned yet" : `at ${clock(stats.peak_at)}`}
        />
      </div>

      {empty ? (
        <p className="mt-4 text-[13px] leading-6 text-zinc-500">
          No schedules are active right now. Once you (or anyone else) schedule loads, their combined power shows up here.
        </p>
      ) : (
        <>
          <PoolChart stats={stats} />
          {stats.peak_to_average !== null && stats.peak_to_average > 0 && (
            <p className="mt-2 font-mono text-[11px] text-zinc-500">
              The busiest moment draws {stats.peak_to_average.toFixed(1)}× the average. The lower this is, the more evenly the load is spread.
            </p>
          )}
        </>
      )}

      <p className="mt-4 text-[13px] leading-6 text-zinc-500">
        When everyone picks the same clean hour, that hour becomes a new peak. Heliotrope nudges each new schedule away from crowded hours.
        {!stats.pool.enabled ? " (That spreading is switched off on this server right now.)" : ""}
      </p>
      <p className="mt-1 font-mono text-[11px] leading-5 text-zinc-700">
        kW is how much power is drawn at one moment; kWh is the electricity used over time.
      </p>
    </div>
  );
}
