"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Attribution, ChangeKind, ChangeStatus, EffectiveStatus, MonitoredState, ObjectType, StructureBadge } from "@/lib/types";
import { Badge } from "@/components/ui/primitives";

type Tone = "green" | "red" | "amber" | "slate" | "blue";

export const CHANGE_KIND: Record<ChangeKind, { label: string; tone: Tone }> = {
  object_added: { label: "Objeto nuevo", tone: "blue" },
  object_removed: { label: "Objeto eliminado", tone: "red" },
  object_modified: { label: "Objeto modificado", tone: "amber" },
};

export const CHANGE_STATUS: Record<ChangeStatus, { label: string; tone: Tone; hint: string }> = {
  pending: { label: "Pendiente", tone: "amber", hint: "Diferencia contra la línea base aprobada, sin revisar." },
  acknowledged: { label: "Entendido", tone: "green", hint: "Se dio por entendido: la diferencia se incorporó a la línea base." },
  superseded: { label: "Reemplazado", tone: "slate", hint: "El objeto volvió a cambiar antes de revisarlo: hay una alerta más reciente." },
  reverted: { label: "Revertido", tone: "slate", hint: "El objeto volvió por sí solo a coincidir con la línea base." },
  out_of_scope: { label: "Fuera de alcance", tone: "slate", hint: "El esquema se excluyó del monitoreo; la alerta se cerró (queda en el historial)." },
};

export const ATTRIBUTION: Record<Attribution, string> = {
  client: "Modificó cliente",
  nexus: "Modificó equipo Nexus",
};

export const OBJECT_TYPE: Record<ObjectType, string> = {
  table: "Tabla",
  view: "Vista",
  matview: "Vista materializada",
  foreign_table: "Tabla foránea",
};

export const EFFECTIVE_STATUS: Record<EffectiveStatus, { label: string; tone: Tone }> = {
  verified: { label: "Verificada", tone: "green" },
  partial: { label: "Verificada parcialmente", tone: "amber" },
  unverifiable: { label: "No se pudo verificar la estructura", tone: "red" },
  never: { label: "Sin inventario aún", tone: "slate" },
  disabled: { label: "Monitoreo deshabilitado", tone: "slate" },
  duplicate: { label: "Duplicada (misma base que otra)", tone: "slate" },
};

export const MONITORED_STATE: Record<MonitoredState, { label: string; tone: Tone }> = {
  awaiting_first_snapshot: { label: "Esperando primer inventario", tone: "slate" },
  baseline_pending: { label: "Línea base por aprobar", tone: "amber" },
  monitoring: { label: "Monitoreando", tone: "green" },
};

export const CHANGE_TYPE_LABEL: Record<string, string> = {
  object_added: "Objeto nuevo",
  object_removed: "Objeto eliminado",
  object_modified: "Objeto modificado",
  column_added: "Columna agregada",
  column_removed: "Columna eliminada",
  column_type_changed: "Tipo de columna",
  column_nullability_changed: "Nulabilidad",
  column_default_changed: "Valor por defecto",
  column_attr_changed: "Atributo de columna",
  constraint_added: "Restricción agregada",
  constraint_removed: "Restricción eliminada",
  constraint_changed: "Restricción modificada",
  index_added: "Índice agregado",
  index_removed: "Índice eliminado",
  index_changed: "Índice modificado",
  view_definition_changed: "Definición de vista",
  object_attr_changed: "Atributo del objeto",
};

export const CHANGE_EVENT_LABEL: Record<string, string> = {
  detected: "Detectado",
  superseded: "Reemplazado por un cambio más reciente",
  reverted: "Revertido (volvió a la línea base)",
  acknowledged: "Dado por entendido",
  reclassified: "Reclasificado",
  evidence_attached: "Evidencia técnica adjunta",
  baseline_reset: "Línea base reiniciada",
  definitions_viewed: "SQL de la definición consultado",
  out_of_scope: "Cerrado: esquema fuera de alcance",
};

export const MDB_EVENT_LABEL: Record<string, string> = {
  created: "Alta",
  config_changed: "Configuración modificada",
  lease_acquired: "Responsable del inventario asignado",
  lease_released: "Responsable liberado",
  baseline_proposed: "Propuesta de línea base",
  baseline_approved: "Línea base aprobada",
  baseline_reset: "Línea base reiniciada",
  verification_failed: "No se pudo verificar la estructura",
  verification_recovered: "Estructura verificada de nuevo",
  duplicate_detected: "Duplicada detectada",
  identity_changed: "Cambió el servidor detrás de la dirección",
  scan_requested: "Inventario solicitado",
  identity_rebound: "Misma base con otra configuración: se conservó la línea base",
  duplicate_undone: "Detección de duplicado deshecha",
  possible_duplicate: "Posible duplicado (solo identidad débil; no se fusiona)",
  view_definitions_purged: "SQL de vistas borrado",
};

export const REASON_LABEL: Record<string, string> = {
  SCHEMAS_UNVERIFIABLE: "Esquemas sin permiso de lectura (no verificables)",
  ENGINE_UNSUPPORTED: "Motor aún no soportado por el inventario",
  ENGINE_IDENTITY_CHANGED: "Respondió otro servidor en la misma dirección",
  ENGINE_IDENTITY_UNAVAILABLE: "No se pudo identificar el servidor",
  TOO_MANY_OBJECTS: "Demasiados objetos (límite del inventario)",
  EMPTY_SNAPSHOT_SUSPICIOUS: "Inventario vacío cuando había objetos (no se infieren eliminaciones)",
  PAYLOAD_TOO_LARGE: "Inventario demasiado grande para enviarlo",
};

export function reasonText(code: string | null | undefined): string {
  if (!code) return "";
  return REASON_LABEL[code] ?? code;
}

export function ChangeKindBadge({ kind }: { kind: ChangeKind }) {
  const k = CHANGE_KIND[kind];
  return <Badge tone={k.tone}>{k.label}</Badge>;
}

export function ChangeStatusBadge({ status }: { status: ChangeStatus }) {
  const s = CHANGE_STATUS[status];
  return (
    <span title={s.hint}>
      <Badge tone={s.tone}>{s.label}</Badge>
    </span>
  );
}

export function EffectiveStatusBadge({ status }: { status: EffectiveStatus }) {
  const s = EFFECTIVE_STATUS[status] ?? { label: status, tone: "slate" as Tone };
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

/** Contador de cambios estructurales pendientes (sondeo cada 30 s). Separado de las incidencias. */
export function useStructureBadge(enabled = true, intervalMs = 30_000) {
  const [badge, setBadge] = useState<StructureBadge | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const load = async () => {
      try {
        const b = await api<StructureBadge>("admin/structural-changes/badge");
        if (alive) setBadge(b);
      } catch {
        /* silencioso */
      }
    };
    void load();
    const id = setInterval(load, intervalMs);
    const onChange = () => void load();
    window.addEventListener("focus", onChange);
    window.addEventListener("dwh:structure-changed", onChange);
    return () => {
      alive = false;
      clearInterval(id);
      window.removeEventListener("focus", onChange);
      window.removeEventListener("dwh:structure-changed", onChange);
    };
  }, [enabled, intervalMs]);
  return badge;
}

export function notifyStructureChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("dwh:structure-changed"));
}

/** Valor legible de un elemento del detalle (columna, restricción, índice…). */
export function fmtDiffValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    if ("type" in o && "not_null" in o) {
      const parts = [String(o.type), o.not_null ? "NOT NULL" : "NULL"];
      if (o.default) parts.push(`DEFAULT ${String(o.default)}`);
      if (o.generated) parts.push(`GENERATED ${String(o.generated)}`);
      if (o.identity) parts.push(`IDENTITY ${String(o.identity)}`);
      if (o.collation) parts.push(`COLLATE ${String(o.collation)}`);
      return parts.join(" · ");
    }
    if ("definition" in o) return `${o.unique ? "UNIQUE " : ""}${String(o.definition)}`;
    if (Object.keys(o).length === 0) return "—";
    return JSON.stringify(o);
  }
  return String(v);
}
