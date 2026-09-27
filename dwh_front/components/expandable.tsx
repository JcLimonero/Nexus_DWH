"use client";

import { useState } from "react";

/** Texto largo (tracebacks) colapsable. */
export function ExpandableText({ text, lines = 2 }: { text: string | null; lines?: number }) {
  const [open, setOpen] = useState(false);
  if (!text) return <span className="text-slate-400">—</span>;
  const long = text.length > 140 || text.includes("\n");
  return (
    <div className="max-w-xl">
      <pre
        className={`whitespace-pre-wrap break-words font-mono text-xs text-slate-600 ${open ? "" : "overflow-hidden"}`}
        style={open ? undefined : { display: "-webkit-box", WebkitLineClamp: lines, WebkitBoxOrient: "vertical" }}
      >
        {text}
      </pre>
      {long && (
        <button type="button" onClick={() => setOpen((o) => !o)} className="mt-1 text-xs font-medium text-brand-600 hover:text-brand-700">
          {open ? "Ver menos" : "Ver más"}
        </button>
      )}
    </div>
  );
}
