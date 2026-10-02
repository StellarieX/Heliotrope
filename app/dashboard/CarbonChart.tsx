"use client";

import { useMemo } from "react";
import type { CarbonSignalResponse } from "../../lib/api/types";

/** Props-driven carbon curve. Dark, responsive, always labels its signal kind. */
export default function CarbonChart({ signal }: { signal: CarbonSignalResponse }) {
  const W = 600;
  const H = 140;
  const values = useMemo(() => signal.points.map((p) => p.carbon_intensity_gco2_per_kwh), [signal]);
  const max = Math.max(...values, 1);
  const min = Math.min(...values, max);
  const span = Math.max(max - min, 1);
  const line = useMemo(
    () =>
      values
        .map((v, i) => {
          const x = (i / Math.max(values.length - 1, 1)) * W;
          const y = H - ((v - min) / span) * (H - 10) - 5;
          return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
        })
        .join(" "),
    [values, min, span]
  );
  const synthetic = signal.signal_type === "SYNTHETIC";

  return (
    <div>
      <div className="flex items-center justify-between">
        <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
          Grid signal · gCO₂/kWh
        </p>
        <span
          className={`rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider ${
            synthetic ? "bg-white/10 text-zinc-300" : "bg-lime-300/15 text-lime-300"
          }`}
          title={`source: ${signal.source}`}
        >
          {synthetic ? "Synthetic signal" : signal.signal_type}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="mt-3 h-32 w-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Carbon intensity over 24 hours, ${signal.points.length} points, peak ${Math.round(max)} grams CO2 per kilowatt-hour`}
      >
        <path d={`${line} L${W},${H} L0,${H} Z`} fill="#fff" opacity="0.06" />
        <path d={line} fill="none" stroke="#a3e635" strokeWidth="1.8" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-1 flex justify-between font-mono text-[10px] text-zinc-600">
        <span>{new Date(signal.start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
        <span>min {Math.round(min)} · max {Math.round(max)} gCO₂/kWh</span>
        <span>{new Date(signal.end).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </div>
    </div>
  );
}
