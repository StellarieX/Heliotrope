"use client";

import type { ExecutionState, ScheduleHistory } from "../../lib/api/types";

const STATUS_STYLE: Record<string, string> = {
  PENDING: "bg-white/5 text-zinc-400",
  READY: "bg-white/10 text-zinc-200",
  RUNNING: "bg-lime-300/15 text-lime-300",
  PAUSED: "bg-orange-400/15 text-orange-300",
  COMPLETED: "bg-white/10 text-zinc-300",
  MISSED: "bg-red-500/15 text-red-300",
  FAILED: "bg-red-500/15 text-red-300",
  CANCELLED: "bg-white/5 text-zinc-600",
};

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Live execution truth, rendered from backend state only. No local guesses. */
export default function ExecutionPanel({
  state,
  history,
  names = {},
  onAdvance,
  onReplan,
  onEvent,
  busy,
}: {
  state: ExecutionState;
  history: ScheduleHistory | null;
  /** job_id -> display name; ids are opaque database keys, never show them raw. */
  names?: Record<string, string>;
  onAdvance: () => void;
  onReplan: () => void;
  onEvent: (jobId: string, type: string) => void;
  busy: boolean;
}) {
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="live-dot h-1.5 w-1.5 rounded-full bg-lime-300" />
          <p className="text-[15px] font-medium">
            {state.lifecycle} <span className="font-mono text-[12px] text-zinc-500">· v{state.version}</span>
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={onAdvance}
            disabled={busy}
            title="Move the simulated clock forward 15 minutes"
            className="cursor-pointer rounded-full border border-white/15 px-4 py-1.5 text-[12px] text-zinc-300 transition hover:border-white/40 hover:text-white active:scale-[0.97] disabled:cursor-wait disabled:opacity-50"
          >
            +15 min
          </button>
          <button
            onClick={onReplan}
            disabled={busy}
            title="Re-optimise whatever hasn't run yet"
            className="cursor-pointer rounded-full bg-lime-300 px-4 py-1.5 text-[12px] font-medium text-black transition hover:bg-lime-200 active:scale-[0.97] disabled:cursor-wait disabled:opacity-50"
          >
            Replan
          </button>
        </div>
      </div>

      <ul className="mt-4 divide-y divide-white/5 border-y border-white/10">
        {state.jobs.map((j) => {
          const pct =
            j.expected_energy_kwh > 0
              ? Math.min(100, (j.energy_delivered_kwh / j.expected_energy_kwh) * 100)
              : j.status === "COMPLETED"
                ? 100
                : 0;
          return (
            <li key={j.job_id} className="py-3">
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-sm font-medium">{names[j.job_id] ?? j.job_id}</p>
                    <span className={`rounded-full px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${STATUS_STYLE[j.status] ?? "bg-white/5 text-zinc-400"}`}>
                      {j.status.toLowerCase()}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-[11px] text-zinc-500">
                    {fmtTime(j.scheduled_start)} → {fmtTime(j.scheduled_end)} · {j.energy_delivered_kwh.toFixed(2)}/{j.expected_energy_kwh.toFixed(2)} kWh
                  </p>
                  <div className="mt-1.5 h-1 max-w-md overflow-hidden rounded-full bg-white/10">
                    <div className="h-full rounded-full bg-lime-300 transition-[width] duration-500" style={{ width: `${pct}%` }} />
                  </div>
                </div>
                <div className="flex shrink-0 gap-1.5">
                  {(j.status === "PENDING" || j.status === "MISSED") && (
                    <button onClick={() => onEvent(j.job_id, "JOB_STARTED")} disabled={busy} className="cursor-pointer rounded-full border border-white/15 px-3 py-1 font-mono text-[10px] text-zinc-300 transition hover:border-white/40 hover:text-white disabled:opacity-50">
                      start
                    </button>
                  )}
                  {j.status === "RUNNING" && (
                    <button onClick={() => onEvent(j.job_id, "JOB_COMPLETED")} disabled={busy} className="cursor-pointer rounded-full border border-white/15 px-3 py-1 font-mono text-[10px] text-zinc-300 transition hover:border-white/40 hover:text-white disabled:opacity-50">
                      done
                    </button>
                  )}
                  {!["COMPLETED", "CANCELLED"].includes(j.status) && (
                    <button onClick={() => onEvent(j.job_id, "JOB_FAILED")} disabled={busy} className="cursor-pointer rounded-full border border-white/15 px-3 py-1 font-mono text-[10px] text-zinc-600 transition hover:border-red-500/40 hover:text-red-300 disabled:opacity-50">
                      fail
                    </button>
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      {history && history.versions.length > 1 && (
        <div className="mt-4">
          <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-zinc-600">Schedule updated</p>
          {history.versions.slice(1).map((v) => (
            <div key={v.version} className="mt-2 rounded-xl border border-white/10 p-4">
              <p className="font-mono text-[11px] text-zinc-500">
                v{v.version} · {v.reason} · {v.solver_status}
              </p>
              {v.changed_jobs.map((c) => (
                <p key={c.job_id} className="mt-1.5 text-[13px] text-zinc-300">
                  {names[c.job_id] ?? c.job_id}: {c.previous_start ? fmtTime(c.previous_start) : "—"} → {c.new_start ? fmtTime(c.new_start) : "—"}
                  <span className="mt-0.5 block text-[12px] text-zinc-500">{c.reason}</span>
                </p>
              ))}
            </div>
          ))}
        </div>
      )}
      <p className="mt-3 font-mono text-[10px] text-zinc-700">simulation — not device telemetry</p>
    </div>
  );
}
