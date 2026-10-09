"use client";

import { useEffect, useMemo, useState } from "react";
import type { CarbonSignalResponse } from "../../lib/api/types";
import type { CarbonForecastResponse, ForecastMode } from "../../lib/api/client";
import { FORECAST_MODE_LABEL as MODE_LABEL, signalTypeLabel } from "./labels";
import Hint from "./Hint";

const MODE_HELP: Record<ForecastMode, string> = {
  ACTUAL: "Now: plans with the grid signal as it is reported today.",
  EXPECTED: "Forecast: plans against the most likely forecast for the next 24 hours.",
  ROBUST: "Cautious: plans against the high end of the forecast, so a dirtier-than-expected grid hurts less.",
};
const CAUTION_LABEL: Record<number, string> = { 0.5: "Medium", 1: "High" };

function clock(iso: string) {
  const d = new Date(iso);
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? t : `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
}

export interface CarbonChartProps {
  signal: CarbonSignalResponse;
  forecast?: CarbonForecastResponse | null;
  mode?: ForecastMode;
  onModeChange?: (mode: ForecastMode) => void;
  riskWeight?: number;
  onRiskWeightChange?: (weight: number) => void;
}

/** Props-driven carbon curve supporting forecast modes (ACTUAL, EXPECTED, ROBUST)
 *  and empirical prediction intervals. */
export default function CarbonChart({
  signal,
  forecast,
  mode: controlledMode,
  onModeChange,
  riskWeight: controlledRiskWeight,
  onRiskWeightChange,
}: CarbonChartProps) {
  const [internalMode, setInternalMode] = useState<ForecastMode>("ACTUAL");
  const [internalRiskWeight, setInternalRiskWeight] = useState<number>(0.5);

  const activeMode = controlledMode ?? internalMode;
  const setMode = (m: ForecastMode) => {
    if (onModeChange) onModeChange(m);
    else setInternalMode(m);
  };

  const activeRiskWeight = controlledRiskWeight ?? internalRiskWeight;
  const setRiskWeight = (w: number) => {
    if (onRiskWeightChange) onRiskWeightChange(w);
    else setInternalRiskWeight(w);
  };

  const W = 600;
  const H = 140;

  const isForecastActive =
    (activeMode === "EXPECTED" || activeMode === "ROBUST") &&
    Boolean(forecast?.points && forecast.points.length > 0);

  const {
    line,
    area,
    lowerLine,
    upperLine,
    robustLine,
    min,
    max,
    startTime,
    endTime,
    label,
  } = useMemo(() => {
    if (isForecastActive && forecast && forecast.points.length > 0) {
      const fPoints = forecast.points;
      const lowers = fPoints.map((p) => p.lower_gco2_per_kwh);
      const uppers = fPoints.map((p) => p.upper_gco2_per_kwh);
      const predicteds = fPoints.map((p) => p.predicted_gco2_per_kwh);
      const robusts = fPoints.map(
        (p) =>
          p.predicted_gco2_per_kwh +
          activeRiskWeight * (p.upper_gco2_per_kwh - p.predicted_gco2_per_kwh)
      );

      const rawMin = Math.min(...lowers, ...predicteds, ...robusts);
      const rawMax = Math.max(...uppers, ...predicteds, ...robusts);
      const pad = Math.max((rawMax - rawMin) * 0.1, 1);
      const minVal = rawMin - pad;
      const maxVal = rawMax + pad;
      const spanVal = Math.max(maxVal - minVal, 1);

      const x = (i: number) => (i / Math.max(fPoints.length - 1, 1)) * W;
      const y = (v: number) => H - ((v - minVal) / spanVal) * (H - 14) - 7;

      const pathFor = (vals: number[]) =>
        vals
          .map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
          .join(" ");

      const upperPath = fPoints
        .map(
          (p, i) =>
            `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.upper_gco2_per_kwh).toFixed(1)}`
        )
        .join(" ");
      const lowerPathRev = [...fPoints]
        .reverse()
        .map((p, i) => {
          const origIdx = fPoints.length - 1 - i;
          return `L${x(origIdx).toFixed(1)},${y(p.lower_gco2_per_kwh).toFixed(1)}`;
        })
        .join(" ");
      const intervalArea = `${upperPath} ${lowerPathRev} Z`;

      return {
        line: pathFor(predicteds),
        area: intervalArea,
        lowerLine: pathFor(lowers),
        upperLine: pathFor(uppers),
        robustLine: pathFor(robusts),
        min: rawMin,
        max: rawMax,
        startTime: fPoints[0]?.timestamp ?? signal.start,
        endTime: fPoints[fPoints.length - 1]?.timestamp ?? signal.end,
        label: `Carbon forecast (${MODE_LABEL[activeMode]}), ${fPoints.length} points, highest ${Math.round(rawMax)} grams of CO2 per kilowatt-hour`,
      };
    }

    const vals = signal.points.map((p) => p.carbon_intensity_gco2_per_kwh);
    if (vals.length === 0) {
      return {
        line: "",
        area: "",
        lowerLine: null,
        upperLine: null,
        robustLine: null,
        min: 0,
        max: 0,
        startTime: signal.start,
        endTime: signal.end,
        label: "No carbon data in range",
      };
    }
    const rawMin = Math.min(...vals);
    const rawMax = Math.max(...vals);
    const pad = rawMax === rawMin ? Math.max(1, Math.abs(rawMax) * 0.05) : (rawMax - rawMin) * 0.1;
    const minVal = rawMin - pad;
    const maxVal = rawMax + pad;
    const spanVal = Math.max(maxVal - minVal, 1);
    const x = (i: number) => (i / Math.max(vals.length - 1, 1)) * W;
    const y = (v: number) => H - ((v - minVal) / spanVal) * (H - 10) - 5;
    const linePath = vals
      .map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
      .join(" ");

    return {
      line: linePath,
      area: `${linePath} L${W},${H} L0,${H} Z`,
      lowerLine: null,
      upperLine: null,
      robustLine: null,
      min: rawMin,
      max: rawMax,
      startTime: signal.start,
      endTime: signal.end,
      label: `Grid carbon, ${signal.points.length} points, highest ${Math.round(rawMax)} grams of CO2 per kilowatt-hour`,
    };
  }, [isForecastActive, forecast, signal, activeRiskWeight, activeMode]);

  const synthetic = signal.signal_type === "SYNTHETIC";
  const estimated = signal.signal_type === "PROXY";
  // "Now" moves once a minute so the marker stays honest on a tab left open.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);
  const spanMs = new Date(endTime).getTime() - new Date(startTime).getTime();
  const nowFrac = spanMs > 0 ? (nowMs - new Date(startTime).getTime()) / spanMs : -1;
  const showNow = !isForecastActive && nowFrac > 0.02 && nowFrac < 0.98;
  const empty = !isForecastActive && signal.points.length === 0;

  if (empty) {
    return (
      <div>
        <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
          Grid carbon · grams of CO₂ per kWh
        </p>
        <p className="mt-4 font-mono text-[12px] text-zinc-600">No grid data for this time range.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-500">
            Grid carbon · g CO₂ per kWh
            <Hint text="How much CO₂ is released to make each unit of electricity (kWh). Lower is cleaner, so the planner runs your loads in the dips. The dashed line marks now." />
          </p>
          <div role="group" aria-label="Which signal to plan with" className="mt-3 flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
            {(["ACTUAL", "EXPECTED", "ROBUST"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                type="button"
                aria-pressed={activeMode === m}
                title={MODE_HELP[m]}
                className={`min-h-10 cursor-pointer rounded-lg px-3.5 py-2 transition ${
                  activeMode === m
                    ? "bg-lime-300 font-semibold text-black"
                    : "bg-white/5 text-zinc-400 hover:bg-white/10 hover:text-white"
                }`}
              >
                {MODE_LABEL[m]}
              </button>
            ))}
            {activeMode === "ROBUST" && (
              <div className="ml-2 flex items-center gap-1.5 text-zinc-400" title="How far toward the top of the forecast range to plan.">
                <span className="text-[10px]">Caution:</span>
                {[0.5, 1.0].map((w) => (
                  <button
                    key={w}
                    type="button"
                    onClick={() => setRiskWeight(w)}
                    aria-pressed={activeRiskWeight === w}
                    className={`min-h-10 min-w-10 cursor-pointer rounded px-3 py-2 text-[11px] ${
                      activeRiskWeight === w
                        ? "bg-amber-400 font-bold text-black"
                        : "bg-white/5 hover:bg-white/10"
                    }`}
                  >
                    {CAUTION_LABEL[w] ?? w}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-col items-end gap-1">
          <span
            className={`rounded-full px-2.5 py-1 font-mono text-[10px] uppercase tracking-wider ${
              isForecastActive
                ? "bg-sky-400/15 text-sky-300"
                : synthetic
                  ? "bg-orange-400/15 text-orange-300"
                  : "bg-lime-300/15 text-lime-300"
            }`}
            title={
              isForecastActive
                ? `Forecast model: ${forecast?.provenance.model}.${forecast ? ` The likely range covers ${Math.round(forecast.provenance.interval_nominal_coverage * 100)}% of past outcomes.` : ""}`
                : `source: ${signal.source}`
            }
          >
            {isForecastActive
              ? activeMode === "ROBUST"
                ? `Cautious forecast · ${(CAUTION_LABEL[activeRiskWeight] ?? String(activeRiskWeight)).toLowerCase()} caution`
                : "Forecast"
              : synthetic
                ? "Test data"
                : estimated
                  ? "Live weather estimate"
                  : signalTypeLabel(signal.signal_type)}
          </span>
          {isForecastActive && (
            <div className="flex items-center gap-3 font-mono text-[10px] text-zinc-500">
              <span className="flex items-center gap-1">
                <i className="block h-[3px] w-3 rounded bg-sky-400 not-italic" /> forecast
              </span>
              <span className="flex items-center gap-1">
                <i className="block h-[5px] w-3 rounded border border-dashed border-sky-400/70 bg-sky-400/20 not-italic" /> likely range
              </span>
              {activeMode === "ROBUST" && (
                <span className="flex items-center gap-1">
                  <i className="block h-[3px] w-3 rounded bg-amber-400 not-italic" /> cautious
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="mt-3 h-32 w-full"
        preserveAspectRatio="none"
        role="img"
        aria-label={label}
      >
        {isForecastActive ? (
          <>
            {/* Prediction interval band */}
            <path d={area} fill="#38bdf8" opacity="0.18" />
            {/* Lower prediction bound */}
            {lowerLine && (
              <path
                d={lowerLine}
                fill="none"
                stroke="#38bdf8"
                strokeWidth="1.2"
                strokeDasharray="4 3"
                opacity="0.6"
                vectorEffect="non-scaling-stroke"
              />
            )}
            {/* Upper prediction bound */}
            {upperLine && (
              <path
                d={upperLine}
                fill="none"
                stroke="#38bdf8"
                strokeWidth="1.2"
                strokeDasharray="4 3"
                opacity="0.6"
                vectorEffect="non-scaling-stroke"
              />
            )}
            {/* Point forecast */}
            <path
              d={line}
              fill="none"
              stroke="#38bdf8"
              strokeWidth={activeMode === "ROBUST" ? "1.2" : "2"}
              opacity={activeMode === "ROBUST" ? "0.6" : "1"}
              vectorEffect="non-scaling-stroke"
            />
            {/* Robust risk-adjusted line */}
            {activeMode === "ROBUST" && robustLine && (
              <path
                d={robustLine}
                fill="none"
                stroke="#fbbf24"
                strokeWidth="2.2"
                vectorEffect="non-scaling-stroke"
              />
            )}
          </>
        ) : (
          <>
            <path d={area} fill="#fff" opacity="0.06" />
            <path
              d={line}
              fill="none"
              stroke="#a3e635"
              strokeWidth="1.8"
              vectorEffect="non-scaling-stroke"
            />
            {showNow && (
              <line
                x1={nowFrac * W}
                x2={nowFrac * W}
                y1="0"
                y2={H}
                stroke="#fff"
                strokeWidth="1"
                strokeDasharray="3 3"
                opacity="0.45"
                vectorEffect="non-scaling-stroke"
              />
            )}
          </>
        )}
      </svg>

      <div className="mt-1 flex flex-wrap justify-between gap-x-3 gap-y-0.5 font-mono text-[10px] text-zinc-600">
        <span>{clock(startTime)}</span>
        <span className="order-last w-full text-center sm:order-none sm:w-auto">
          lowest {Math.round(min)} · highest {Math.round(max)}
        </span>
        <span>{clock(endTime)}</span>
      </div>
      {activeMode !== "ACTUAL" && !isForecastActive && (
        <p className="mt-2 text-[12px] leading-5 text-zinc-600" aria-live="polite">
          No forecast available right now, so the chart shows the current signal.
        </p>
      )}
    </div>
  );
}
