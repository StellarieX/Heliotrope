"use client";

import { useEffect, useState } from "react";
import { classifyLoad } from "../api/client";
import type { JobType } from "../api/types";

/** What the backend decided a load is. */
export interface JevAnswer {
  jobType: JobType;
  category: string;
  /** 0 to 1. */
  confidence: number;
  /** "jev" when the Jev model answered, "rules" when the built-in rules did. */
  provider: "jev" | "rules";
  ambiguous: boolean;
}

export type ClassifyState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "ok"; answer: JevAnswer }
  /** The backend did not answer; callers fall back to the local guess and say so. */
  | { status: "offline" };

const DEBOUNCE_MS = 400;

// Answers are deterministic for a given name, so a preset added twice (or a name
// retyped) does not cost another request.
const cache = new Map<string, JevAnswer>();

/**
 * Debounced backend classification of a free-text load name. The answer is only
 * returned while it still matches the current text, so a slow reply for an old
 * name never flashes the wrong type.
 */
export function useJevClassify(name: string): ClassifyState {
  const text = name.trim();
  const key = text.toLowerCase();
  const [slot, setSlot] = useState<{ forKey: string; answer: JevAnswer | null } | null>(null);

  useEffect(() => {
    if (key.length < 2 || cache.has(key)) return;
    let cancelled = false;
    const t = setTimeout(() => {
      classifyLoad({ name: text })
        .then((r) => {
          const answer: JevAnswer = {
            jobType: r.classification.job_type,
            category: r.classification.category,
            confidence: r.confidence,
            provider: r.provider === "jev" ? "jev" : "rules",
            ambiguous: r.ambiguous,
          };
          cache.set(key, answer);
          if (!cancelled) setSlot({ forKey: key, answer });
        })
        .catch(() => {
          if (!cancelled) setSlot({ forKey: key, answer: null });
        });
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [key, text]);

  if (key.length < 2) return { status: "idle" };
  const hit = cache.get(key);
  if (hit) return { status: "ok", answer: hit };
  if (slot && slot.forKey === key) return slot.answer ? { status: "ok", answer: slot.answer } : { status: "offline" };
  return { status: "checking" };
}
