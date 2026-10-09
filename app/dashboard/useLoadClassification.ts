"use client";

import { useEffect, useState } from "react";
import { classifyLoad } from "../../lib/api/client";
import type { JobType } from "../../lib/api/types";

export interface LoadClassification {
  jobType: JobType;
  category: string;
  confidence: number;
  ambiguous: boolean;
  /** "jev" when Jev decided; "rule_based" when the backend's built-in rules did. */
  provider: string;
  /** Set when Jev was asked but the built-in rules answered instead, with why. */
  fallbackReason: string | null;
}

export type ClassifyStatus =
  /** Nothing typed yet. */
  | "idle"
  /** Waiting for the backend (includes the short typing pause). */
  | "loading"
  /** The backend answered for exactly this text. */
  | "ready"
  /** The backend could not be reached for this text. */
  | "unavailable";

export interface ClassifyView {
  status: ClassifyStatus;
  result: LoadClassification | null;
}

const DEBOUNCE_MS = 400;

type Entry = { forName: string; result: LoadClassification | null };

/**
 * Asks the backend (Jev when it has a key, otherwise its built-in rules) what kind of
 * load a typed name is. Debounced, and an answer for an older name is never shown for a
 * newer one. `enabled` is false while the backend is not reachable: no request is made
 * and the status is "unavailable" so the caller can say it is only guessing.
 */
export function useLoadClassification(name: string, enabled: boolean): ClassifyView {
  const text = name.trim();
  const [entry, setEntry] = useState<Entry | null>(null);

  useEffect(() => {
    if (text.length < 2 || !enabled) return;
    let cancelled = false;
    const t = setTimeout(() => {
      classifyLoad({ name: text })
        .then((r) => {
          if (cancelled) return;
          const fb = r.assumptions.find((a) => a.field === "classifier");
          setEntry({
            forName: text,
            result: {
              jobType: r.classification.job_type,
              category: r.classification.category,
              confidence: r.confidence,
              ambiguous: r.ambiguous,
              provider: r.provider,
              fallbackReason: fb ? fb.detail : null,
            },
          });
        })
        .catch(() => {
          if (!cancelled) setEntry({ forName: text, result: null });
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [text, enabled]);

  if (text.length < 2) return { status: "idle", result: null };
  if (!enabled) return { status: "unavailable", result: null };
  if (!entry || entry.forName !== text) return { status: "loading", result: null };
  return entry.result ? { status: "ready", result: entry.result } : { status: "unavailable", result: null };
}
