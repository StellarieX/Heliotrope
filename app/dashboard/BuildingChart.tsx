"use client";

import { useMemo } from "react";
import type { CoordinationAggregatePoint } from "../../lib/api/types";

/** Building aggregate: baseline + flexible + capacity ceiling + congestion.
 *  Props-driven like CarbonChart; the backend computes, this only draws. */
export default function BuildingChart({ points }: { points: CoordinationAggregatePoint[] }) {
  const W = 600;
  const H = 150;
  const safePoints = useMemo(() => points ?? [], [points]);
  const maxY = useMemo(
    () => Math.max(1, ...safePoints.map((p) => Math.max(p.total_kw, p.capacity_kw))),
    [safePoints]
  );
  const { cap, total, flex } = useMemo(() => {
    if (safePoints.length === 0) return { cap: "", total: "", flex: "" };
    const x = (i: number) => (i / Math.max(safePoints.length - 1, 1)) * W;
    const y = (v: number) => H - (v / maxY) * (H - 12) - 6;
    const line = (pick: (p: CoordinationAggregatePoint) => number) =>
      safePoints
        .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(pick(p)).toFixed(1)}`)
        .join(" ");
    return {
      cap: line((p) => p.capacity_kw),
      total: line((p) => p.total_kw),
      flex: line((p) => p.flexible_kw),
    };
  }, [safePoints, maxY]);
  const peak = useMemo(() => Math.max(0, ...safePoints.map((p) => p.total_kw)), [safePoints]);

  if (safePoints.length === 0) {
    return (
      <div>
        <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
          Building load · kW
        </p>
        <p className="mt-4 font-mono text-[12px] text-zinc-600">No building coordination profile computed yet.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
          Building load · kW
        </p>
        <div className="flex items-center gap-3 font-mono text-[10px] text-zinc-500">
          <span className="flex items-center gap-1"><i className="block h-[3px] w-4 rounded bg-lime-300 not-italic" /> total</span>
          <span className="flex items-center gap-1"><i className="block h-[3px] w-4 rounded bg-zinc-500 not-italic" /> flexible</span>
          <span className="flex items-center gap-1"><i className="block h-[3px] w-4 rounded bg-white/40 not-italic" /> capacity</span>
        </div>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="mt-3 h-36 w-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Building aggregate load, peak ${peak.toFixed(1)} kilowatts`}
      >
        <path d={`${total} L${W},${H} L0,${H} Z`} fill="#a3e635" opacity="0.08" />
        <path d={cap} fill="none" stroke="#fff" strokeWidth="1.2" strokeDasharray="5 4" opacity="0.45" vectorEffect="non-scaling-stroke" />
        <path d={flex} fill="none" stroke="#71717a" strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
        <path d={total} fill="none" stroke="#a3e635" strokeWidth="1.8" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-1 flex justify-between font-mono text-[10px] text-zinc-600">
        <span>peak {peak.toFixed(1)} kW</span>
        <span>{points.length} slots</span>
      </div>
    </div>
  );
}
