"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import type { TaskHealth } from "@/lib/types";
import { cx, fmtSeconds } from "@/lib/format";
import { Badge } from "@/components/ui/primitives";
import { CATEGORY_LABEL, TASK_STATE, fmtSecs } from "@/components/health";

/**
 * Piezas compartidas por las vistas "Grupo → extractores por agencia" y "Agencia → extractores".
 * Un extractor es una tarea (agency_task); su estado sale de GET /admin/health/tasks.
 */

/** Mismo criterio que el filtro `state` del backend: "Retrasada" incluye las que además fallan o corren. */
export function matchesState(t: TaskHealth, state: string): boolean {
  if (!state) return true;
  return t.state === state || (state === "delayed" && t.delayed);
}

export function matchesSearch(t: TaskHealth, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return [t.object_name, t.destination_table, t.agency_name, t.company_name, `#${t.task_id}`].some((x) =>
    (x ?? "").toLowerCase().includes(needle),
  );
}

export interface ExtractorSummary {
  total: number;
  active: number;
  failing: number;
  delayed: number;
  neverRun: number;
}

export function summarize(items: TaskHealth[]): ExtractorSummary {
  return {
    total: items.length,
    active: items.filter((t) => t.effective_active).length,
    failing: items.filter((t) => t.state === "failing").length,
    delayed: items.filter((t) => matchesState(t, "delayed")).length,
    neverRun: items.filter((t) => t.state === "never_run").length,
  };
}

const CARD_TONE = {
  slate: "text-slate-900",
  green: "text-emerald-700",
  red: "text-red-700",
  amber: "text-amber-700",
} as const;

/** Tarjeta de resumen; con `onClick` funciona como atajo de filtro (marcada si está activa). */
export function SummaryCard({
  label,
  value,
  hint,
  tone = "slate",
  active,
  onClick,
}: {
  label: string;
  value: number | string;
  hint?: string;
  tone?: keyof typeof CARD_TONE;
  active?: boolean;
  onClick?: () => void;
}) {
  const body = (
    <>
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className={cx("mt-1 text-2xl font-semibold tabular-nums", CARD_TONE[tone])}>{value}</p>
      {hint && <p className="mt-0.5 truncate text-[11px] text-slate-500">{hint}</p>}
    </>
  );
  const base = "rounded-lg border bg-white p-3 text-left shadow-sm sm:p-4";
  if (!onClick) return <div className={cx(base, "border-slate-200")}>{body}</div>;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={Boolean(active)}
      title={active ? "Quitar filtro" : "Filtrar por este estado"}
      className={cx(
        base,
        "transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
        active ? "border-brand-500 ring-1 ring-brand-500" : "border-slate-200 hover:border-slate-300 hover:bg-slate-50",
      )}
    >
      {body}
    </button>
  );
}

/** "cada 15 min" */
export function everyLabel(seconds: number): string {
  return `cada ${fmtSeconds(seconds)}`;
}

/** Estado de salud del extractor + indicadores extra e incidencias abiertas. */
export function ExtractorState({ t, compact = false }: { t: TaskHealth; compact?: boolean }) {
  const st = TASK_STATE[t.state];
  return (
    <div className="flex flex-col items-start gap-1" title={st.hint}>
      <Badge tone={st.tone}>{st.label}</Badge>
      {t.delayed && t.state !== "delayed" && <Badge tone="amber">Retrasada</Badge>}
      {t.running_long && <Badge tone="amber">Prolongada</Badge>}
      {!compact &&
        t.open_incidents.map((i) => (
          <Link key={i.id} href={`/incidencias?id=${i.id}`} className="text-[11px] text-red-700 hover:underline">
            #{i.id} {CATEGORY_LABEL[i.category]}
            {i.acknowledged ? " (reconocida)" : ""}
          </Link>
        ))}
      {compact && t.open_incidents.length > 0 && (
        <span className="text-[11px] text-red-700">{t.open_incidents.length} incidencia(s) abierta(s)</span>
      )}
      {t.running_execution && (
        <p className="text-[11px] text-brand-700">
          {fmtSecs(t.running_execution.elapsed_seconds)} en {t.running_execution.installation_name ?? "—"}
        </p>
      )}
    </div>
  );
}

/** Migas de pan: [{label, href?}] (el último es la página actual). */
export function Breadcrumbs({ items }: { items: { label: ReactNode; href?: string }[] }) {
  return (
    <nav aria-label="Ruta" className="mb-2 min-w-0">
      <ol className="flex flex-wrap items-center gap-1 text-xs text-slate-500">
        {items.map((it, i) => (
          <li key={i} className="flex min-w-0 items-center gap-1">
            {i > 0 && <ChevronRight className="h-3 w-3 shrink-0 text-slate-400" aria-hidden />}
            {it.href ? (
              <Link href={it.href} className="truncate hover:text-brand-700 hover:underline">
                {it.label}
              </Link>
            ) : (
              <span className="truncate font-medium text-slate-700" aria-current="page">
                {it.label}
              </span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}
