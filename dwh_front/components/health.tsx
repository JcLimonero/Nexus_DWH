"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Connectivity, Incident, IncidentBadge, IncidentCategory, Severity, TaskHealthState } from "@/lib/types";
import { Badge } from "@/components/ui/primitives";

type Tone = "green" | "red" | "amber" | "slate" | "blue";

export const SEVERITY: Record<Severity, { label: string; tone: Tone }> = {
  info: { label: "Info", tone: "slate" },
  warning: { label: "Advertencia", tone: "amber" },
  error: { label: "Error", tone: "red" },
  critical: { label: "Crítica", tone: "red" },
};

export const CATEGORY_LABEL: Record<IncidentCategory, string> = {
  disconnected: "Instalación sin contacto",
  task_failed: "Falla de tarea",
  task_delayed: "Tarea retrasada",
  task_running_long: "Ejecución prolongada",
  checkpoint_kind_mismatch: "Watermark de otro reloj",
  queue_dead_letter: "Reportes apartados (cola)",
  queue_overflow: "Descartes en la cola",
};

export const TASK_STATE: Record<TaskHealthState, { label: string; tone: Tone; hint: string }> = {
  ok: { label: "Al día", tone: "green", hint: "Última carga confirmada dentro del plazo esperado." },
  running: { label: "En curso", tone: "blue", hint: "Hay una ejecución en curso (confirmada por el latido)." },
  failing: { label: "Con error", tone: "red", hint: "La ejecución más reciente falló o hay una falla abierta en alguna instalación que ejecuta la tarea." },
  delayed: { label: "Retrasada", tone: "amber", hint: "Sin carga confirmada dentro de periodicidad + duración esperada + tolerancia." },
  never_run: { label: "Sin ejecuciones", tone: "slate", hint: "Aún no se ha ejecutado (dentro del periodo de gracia)." },
  disabled: { label: "Deshabilitada", tone: "slate", hint: "La tarea o su agencia/empresa/grupo/objeto está deshabilitado: no genera alertas de retraso." },
};

export const CONNECTIVITY: Record<Connectivity, { label: string; tone: Tone }> = {
  online: { label: "En línea", tone: "green" },
  offline: { label: "Sin contacto", tone: "red" },
  never: { label: "Nunca contactó", tone: "slate" },
  revoked: { label: "Revocada", tone: "slate" },
  scope_disabled: { label: "Alcance deshabilitado", tone: "slate" },
};

export const TRANSITION_LABEL: Record<string, string> = {
  opened: "Apertura",
  resolved: "Resolución",
  reminder: "Recordatorio",
  test: "Prueba",
};

export const DELIVERY_STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "Pendiente", tone: "amber" },
  sending: { label: "Enviando", tone: "blue" },
  delivered: { label: "Entregada", tone: "green" },
  failed: { label: "Fallida", tone: "red" },
  skipped: { label: "Omitida", tone: "slate" },
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  const s = SEVERITY[severity] ?? SEVERITY.info;
  return <Badge tone={s.tone} className={severity === "critical" ? "font-semibold uppercase" : undefined}>{s.label}</Badge>;
}

/** Estado técnico (Abierta/Resuelta) y reconocimiento se muestran SIEMPRE por separado. */
export function IncidentStatusBadges({ incident }: { incident: Pick<Incident, "status" | "acknowledged"> }) {
  return (
    <div className="flex flex-wrap gap-1">
      {incident.status === "open" ? <Badge tone="red">Abierta</Badge> : <Badge tone="green">Resuelta</Badge>}
      {incident.acknowledged ? <Badge tone="blue">Reconocida</Badge> : <Badge tone="slate">Sin reconocer</Badge>}
    </div>
  );
}

export function fmtSecs(total: number | null | undefined): string {
  if (total === null || total === undefined) return "—";
  const s = Math.max(0, Math.round(total));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min${s % 60 ? ` ${s % 60} s` : ""}`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h${m % 60 ? ` ${m % 60} min` : ""}`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** Contador de incidencias abiertas sin reconocer (sondeo cada 30 s). */
export function useIncidentBadge(enabled = true, intervalMs = 30_000) {
  const [badge, setBadge] = useState<IncidentBadge | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const load = async () => {
      try {
        const b = await api<IncidentBadge>("admin/incidents/badge");
        if (alive) setBadge(b);
      } catch {
        /* silencioso: el badge no debe romper la navegación */
      }
    };
    void load();
    const id = setInterval(load, intervalMs);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    window.addEventListener("dwh:incidents-changed", onFocus);
    return () => {
      alive = false;
      clearInterval(id);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("dwh:incidents-changed", onFocus);
    };
  }, [enabled, intervalMs]);
  return badge;
}

/** Avisa al badge del sidebar que algo cambió (reconocer/cerrar). */
export function notifyIncidentsChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("dwh:incidents-changed"));
}

/** Reloj del que proviene el watermark (se guarda SIN zona: no se convierte). */
export const WM_KIND_SHORT: Record<string, string> = {
  source_clock: "reloj del origen",
  agent_local: "reloj local del agente",
  agent_utc: "UTC del agente",
  legacy_last_run: "legado",
};

/** Recarga periódica (p. ej. 30 s) mientras la pestaña está visible. */
export function useAutoRefresh(reload: () => void, ms = 30_000) {
  useEffect(() => {
    const id = setInterval(() => {
      if (typeof document === "undefined" || document.visibilityState === "visible") reload();
    }, ms);
    return () => clearInterval(id);
  }, [reload, ms]);
}
