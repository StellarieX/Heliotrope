"use client";

import { useState } from "react";
import type { ExecutionState, OverrideCommand, ScheduleHistory } from "../../lib/api/types";
import { jobStatusLabel, lifecycleLabel, reasonLabel, solverLabel } from "./labels";

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

/** "7:30 PM", or "Tue 1:00 AM" when it falls on another day, so an overnight run reads correctly. */
function fmtTime(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString()
    ? t
    : `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
}

const actionBtn =
  "min-h-11 cursor-pointer rounded-full border border-white/15 px-4 py-2 text-[12px] text-zinc-300 transition hover:border-white/40 hover:text-white active:scale-[0.97] disabled:cursor-wait disabled:opacity-50";
const quietBtn =
  "min-h-11 cursor-pointer rounded-full px-3 py-2 text-[12px] text-zinc-500 transition hover:text-white active:scale-[0.97] disabled:cursor-wait disabled:opacity-50";
const dangerBtn = `${actionBtn} text-zinc-500 hover:border-red-500/40 hover:text-red-300`;

/** Live execution state, rendered from the backend only. Progress is what the
 *  user reports (no meters are connected); the schedule itself follows the real clock. */
export default function ExecutionPanel({
  state,
  history,
  names = {},
  onReplan,
  onEvent,
  onOverride,
  busy,
}: {
  state: ExecutionState;
  history: ScheduleHistory | null;
  /** job_id -> display name; ids are opaque database keys, never show them raw. */
  names?: Record<string, string>;
  onReplan: () => void;
  onEvent: (jobId: string, type: string) => void;
  onOverride: (jobId: string, command: OverrideCommand, newDeadlineAt?: string) => void;
  busy: boolean;
}) {
  const [confirmCancel, setConfirmCancel] = useState<string | null>(null);
  const [moveOpen, setMoveOpen] = useState<string | null>(null);
  // Which row has its secondary actions (give more time, didn't run, cancel) open.
  const [moreOpen, setMoreOpen] = useState<string | null>(null);
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="live-dot h-1.5 w-1.5 rounded-full bg-lime-300" />
          <p className="text-[15px] font-medium">
            {lifecycleLabel(state.lifecycle)} <span className="font-mono text-[12px] text-zinc-500">· version {state.version}</span>
          </p>
        </div>
        <button
          onClick={onReplan}
          disabled={busy}
          title="Re-plan whatever hasn't run yet, starting from now, using the latest grid forecast"
          className="min-h-11 cursor-pointer rounded-full bg-lime-300 px-5 py-2 text-[12px] font-medium text-black transition hover:bg-lime-200 active:scale-[0.97] disabled:cursor-wait disabled:opacity-50"
        >
          Update schedule
        </button>
      </div>

      <ul className="mt-4 divide-y divide-white/5 border-y border-white/10">
        {state.jobs.map((j) => {
          const pct =
            j.expected_energy_kwh > 0
              ? Math.min(100, (j.energy_delivered_kwh / j.expected_energy_kwh) * 100)
              : j.status === "COMPLETED"
                ? 100
                : 0;
          const terminal = ["COMPLETED", "CANCELLED", "FAILED"].includes(j.status);
          const canExtend = !terminal && Boolean(j.deadline_at);
          const isOpen = moreOpen === j.job_id;
          return (
            <li key={j.job_id} className="py-3.5">
              <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2.5">
                <div className="min-w-0 flex-1 basis-48">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-sm font-medium">{names[j.job_id] ?? "Removed load"}</p>
                    <span className={`rounded-full px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${STATUS_STYLE[j.status] ?? "bg-white/5 text-zinc-400"}`}>
                      {jobStatusLabel(j.status)}
                    </span>
                  </div>
                  <p
                    title="kWh (kilowatt-hours) is the electricity used over time, the unit your meter counts."
                    className="mt-1 font-mono text-[11px] text-zinc-500"
                  >
                    {fmtTime(j.scheduled_start)} → {fmtTime(j.scheduled_end)} · {j.energy_delivered_kwh.toFixed(2)} / {j.expected_energy_kwh.toFixed(2)} kWh
                  </p>
                  {j.status === "MISSED" && (
                    <p className="mt-1 text-[12px] leading-5 text-red-300/90">Start it now, or update the schedule for a new time.</p>
                  )}
                  <div className="mt-1.5 h-1 max-w-md overflow-hidden rounded-full bg-white/10">
                    <div className="h-full rounded-full bg-lime-300 transition-[width] duration-500" style={{ width: `${pct}%` }} />
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  {(j.status === "PENDING" || j.status === "READY") && (
                    <>
                      <button onClick={() => onEvent(j.job_id, "JOB_STARTED")} disabled={busy} className={actionBtn}>
                        I started it
                      </button>
                      <button onClick={() => onOverride(j.job_id, "START_NOW")} disabled={busy} className={actionBtn}>
                        Start now
                      </button>
                    </>
                  )}
                  {j.status === "MISSED" && (
                    <button onClick={() => onOverride(j.job_id, "START_NOW")} disabled={busy} className={actionBtn}>
                      Start now
                    </button>
                  )}
                  {j.status === "RUNNING" && (
                    <>
                      <button onClick={() => onEvent(j.job_id, "JOB_COMPLETED")} disabled={busy} className={actionBtn}>
                        It&apos;s done
                      </button>
                      <button onClick={() => onOverride(j.job_id, "PAUSE")} disabled={busy} className={actionBtn}>
                        Pause
                      </button>
                    </>
                  )}
                  {j.status === "PAUSED" && (
                    <button onClick={() => onOverride(j.job_id, "START_NOW")} disabled={busy} className={actionBtn}>
                      Resume
                    </button>
                  )}
                  {!terminal && (
                    <button
                      onClick={() => {
                        setMoreOpen(isOpen ? null : j.job_id);
                        setMoveOpen(null);
                        setConfirmCancel(null);
                      }}
                      aria-expanded={isOpen}
                      className={quietBtn}
                    >
                      {isOpen ? "Less" : "More"}
                    </button>
                  )}
                </div>
              </div>
              {!terminal && isOpen && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {j.status === "MISSED" && (
                    <button onClick={() => onEvent(j.job_id, "JOB_STARTED")} disabled={busy} className={actionBtn}>
                      I started it
                    </button>
                  )}
                  {canExtend && (
                    <button
                      onClick={() => setMoveOpen(moveOpen === j.job_id ? null : j.job_id)}
                      disabled={busy}
                      aria-expanded={moveOpen === j.job_id}
                      title="Allow this load to finish later than planned"
                      className={actionBtn}
                    >
                      Give more time
                    </button>
                  )}
                  <button onClick={() => onEvent(j.job_id, "JOB_FAILED")} disabled={busy} className={dangerBtn}>
                    It didn&apos;t run
                  </button>
                  {confirmCancel !== j.job_id ? (
                    <button onClick={() => setConfirmCancel(j.job_id)} disabled={busy} className={dangerBtn}>
                      Cancel
                    </button>
                  ) : (
                    <>
                      <button
                        onClick={() => {
                          setConfirmCancel(null);
                          setMoreOpen(null);
                          onOverride(j.job_id, "CANCEL");
                        }}
                        disabled={busy}
                        className={`${actionBtn} border-red-500/40 text-red-300 hover:border-red-400 hover:text-red-200`}
                      >
                        Yes, cancel it
                      </button>
                      <button onClick={() => setConfirmCancel(null)} className={actionBtn}>
                        Keep it
                      </button>
                    </>
                  )}
                </div>
              )}
              {canExtend && isOpen && moveOpen === j.job_id && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <span className="text-[12px] text-zinc-500">Finish later, by up to</span>
                  {[1, 3].map((h) => (
                    <button
                      key={h}
                      disabled={busy}
                      onClick={() => {
                        setMoveOpen(null);
                        setMoreOpen(null);
                        onOverride(j.job_id, "MOVE", new Date(Math.max(Date.now(), new Date(j.deadline_at ?? 0).getTime()) + h * 3600_000).toISOString());
                      }}
                      className={actionBtn}
                    >
                      {h} more hour{h === 1 ? "" : "s"}
                    </button>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {history && history.versions.length > 1 && (
        <details className="group mt-3">
          <summary className="inline-flex min-h-10 cursor-pointer list-none items-center gap-2 font-mono text-[11px] text-zinc-500 transition hover:text-white [&::-webkit-details-marker]:hidden">
            <span className="transition group-open:rotate-90">›</span>
            What changed ({history.versions.length - 1})
          </summary>
          {history.versions.slice(1).map((v) => (
            <div key={v.version} className="mt-2 rounded-xl border border-white/10 p-4">
              <p className="font-mono text-[11px] text-zinc-500">
                Version {v.version} · {reasonLabel(v.reason)} · {solverLabel(v.solver_status)}
              </p>
              {v.changed_jobs.map((c) => (
                <p key={c.job_id} className="mt-1.5 text-[13px] text-zinc-300">
                  {names[c.job_id] ?? c.job_id}: {c.previous_start ? fmtTime(c.previous_start) : "—"} → {c.new_start ? fmtTime(c.new_start) : "—"}
                  <span className="mt-0.5 block text-[12px] text-zinc-500">{c.reason}</span>
                </p>
              ))}
            </div>
          ))}
        </details>
      )}
      <p className="mt-3 text-[11px] leading-5 text-zinc-600">Progress is what you report; no meters are connected.</p>
    </div>
  );
}
