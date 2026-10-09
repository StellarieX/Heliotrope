"use client";

import { useEffect, useMemo, useState } from "react";
import { prioritizeLoads } from "../api/client";
import { hoursUntilReady, jevRank, type JobInput, type RankedJob } from "../prioritize";

export type RankingState =
  | { status: "loading" }
  | {
      status: "ok";
      ranked: RankedJob[];
      /** Who produced the order, in words the user can read. */
      label: string;
      provider: "jev" | "mixed" | "heuristic";
    }
  | { status: "fallback"; ranked: RankedJob[]; label: string };

/**
 * Priority order for the loads about to be saved. Asks the backend (Jev judges how
 * essential each appliance is, then the backend adds time pressure, size and how
 * little room the user left). If the backend cannot be reached the local heuristic
 * is used and the label says so.
 */
export function useBackendRanking(jobs: JobInput[], enabled: boolean): RankingState {
  const movable = useMemo(() => jobs.filter((j) => j.shiftable !== false), [jobs]);
  const signature = useMemo(
    () => JSON.stringify(movable.map((j) => [j.id, j.name, j.kind, j.powerKw, j.readyBy, j.flexHours])),
    [movable]
  );
  const [done, setDone] = useState<{ sig: string; state: RankingState } | null>(null);

  useEffect(() => {
    if (!enabled || movable.length === 0) return;
    let cancelled = false;
    prioritizeLoads(
      movable.map((j) => ({
        id: j.id,
        name: j.name,
        kind: j.kind.slice(0, 60),
        power_kw: j.powerKw,
        hours_until_ready: hoursUntilReady(j.readyBy),
        flex_hours: j.flexHours,
      }))
    )
      .then((res) => {
        if (cancelled) return;
        const byId = new Map(movable.map((j) => [j.id, j]));
        const ranked = res.items.flatMap((it): RankedJob[] => {
          const j = byId.get(it.id);
          return j ? [{ ...j, score: it.score, band: it.band, reason: it.reason, source: it.source }] : [];
        });
        const label =
          res.provider === "jev"
            ? "Ordered by Jev (how essential each appliance is) plus time pressure, size and flexibility."
            : res.provider === "mixed"
              ? `Partly ordered by Jev; the rest by built-in rules. ${res.notes[0] ?? ""}`.trim()
              : (res.notes[0] ?? "Ordered by the built-in rules: time pressure, size and flexibility. Jev was not used.");
        setDone({ sig: signature, state: { status: "ok", ranked, label, provider: res.provider } });
      })
      .catch(() => {
        if (cancelled) return;
        setDone({
          sig: signature,
          state: {
            status: "fallback",
            ranked: jevRank(movable),
            label: "Could not reach the backend, so this order comes from the simple rules built into this page.",
          },
        });
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, movable, signature]);

  if (!enabled || movable.length === 0) return { status: "ok", ranked: [], label: "", provider: "heuristic" };
  return done && done.sig === signature ? done.state : { status: "loading" };
}
