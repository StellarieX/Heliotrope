"use client";

import { useEffect, useRef, useState } from "react";

/** A small "i" that explains something on hover (title) or tap (a short note).
 *  Keeps secondary explanations out of the page without losing them. */
export default function Hint({ text, label = "More info" }: { text: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: Event) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <span ref={ref} className="relative inline-flex align-middle">
      <button
        type="button"
        title={text}
        aria-label={label}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="-m-2 grid h-10 w-10 cursor-pointer place-items-center text-zinc-600 transition hover:text-zinc-300"
      >
        <span className="grid h-4 w-4 place-items-center rounded-full border border-current font-serif text-[10px] italic leading-none">i</span>
      </button>
      {open && (
        <span
          role="note"
          className="fixed inset-x-4 bottom-4 z-30 rounded-xl border border-white/15 bg-[#111] px-4 py-3 text-left font-sans text-[12px] font-normal normal-case leading-5 tracking-normal text-zinc-300 shadow-xl shadow-black/60 sm:absolute sm:inset-x-auto sm:bottom-auto sm:left-0 sm:top-full sm:mt-3 sm:w-72"
        >
          {text}
        </span>
      )}
    </span>
  );
}
