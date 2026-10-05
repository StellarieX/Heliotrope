"use client";

import { useEffect, useState } from "react";
import { classifyLoad } from "../api/client";
import type { JobType } from "../api/types";

export interface Classified {
  forName: string;
  jobType: JobType;
  category: string;
  confidence: number;
  ambiguous: boolean;
  /** Which classifier answered: "jev" (TypeSafe's System One model) or "rule_based". */
  provider: string;
  /** Set when the AI classifier was asked but fell back to the rules, with why. */
  fallbackReason: string | null;
}

/**
 * Debounced backend classification of a free-text load name ("borewell pump").
 *
 * The result is only returned while it still matches the current text, so a slow
 * answer for an old name is ignored instead of flashing the wrong type. Returns
 * null while typing, offline, or if the backend is down: callers fall back to
 * the local hint and never block on this.
 */
export function useClassification(name: string, deadlineWall?: string): Classified | null {
  const [result, setResult] = useState<Classified | null>(null);
  const text = name.trim();

  useEffect(() => {
    if (text.length < 2) return;
    let cancelled = false;
    const t = setTimeout(() => {
      classifyLoad({ name: text, ...(deadlineWall ? { deadline_wall: deadlineWall } : {}) })
        .then((r) => {
          if (cancelled) return;
          const fb = r.assumptions.find((a) => a.field === "classifier");
          setResult({
            forName: text,
            jobType: r.classification.job_type,
            category: r.classification.category,
            confidence: r.confidence,
            ambiguous: r.ambiguous,
            provider: r.provider,
            fallbackReason: fb ? fb.detail : null,
          });
        })
        .catch(() => {
          if (!cancelled) setResult(null);
        });
    }, 350);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [text, deadlineWall]);

  return result && result.forName === text ? result : null;
}
