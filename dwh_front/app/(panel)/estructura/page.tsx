"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { CheckCheck, Database, Eye, Info, Lock, Play, Plus, RefreshCw, RotateCcw, ShieldAlert, Unlink } from "lucide-react";
import { ApiError, api, qs, useApi } from "@/lib/api";
import type {
  Attribution,
  BaselineResponse,
  Company,
  ListResponse,
  MonitoredDatabase,
  MonitoredDatabaseDetail,
  ObjectStructure,
  StructuralChange,
  StructuralChangeDetail,
} from "@/lib/types";
import { cx, fmtAgo, fmtDateTz, fmtNumber, fmtSeconds, tzLabel } from "@/lib/format";
import { Badge, Button, Card, Field, IconButton, Input, PageHeader, Select, Switch, Textarea } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { Modal } from "@/components/ui/modal";
import { useConfirm, useToast } from "@/components/ui/feedback";
import { CompanyOptions } from "@/components/filters";
import { useAutoRefresh } from "@/components/health";
import { FilterBar, dateRange, useUrlFilters } from "@/components/scope-filters";
import { useSession } from "@/components/session";

const CHANGE_FILTERS = ["group_id", "company_id", "agency_id", "database_id", "since", "until"] as const;
const DB_FILTERS = ["group_id", "company_id", "agency_id"] as const;

/** Quita un parámetro de la URL conservando los filtros. */
function withoutParam(params: URLSearchParams, key: string): string {
  const next = new URLSearchParams(params.toString());
  next.delete(key);
  const q = next.toString();
  return q ? `/estructura?${q}` : "/estructura";
}
import {
  ATTRIBUTION,
  CHANGE_EVENT_LABEL,
  CHANGE_TYPE_LABEL,
  ChangeKindBadge,
  ChangeStatusBadge,
  EffectiveStatusBadge,
  MDB_EVENT_LABEL,
  MONITORED_STATE,
  OBJECT_TYPE,
  fmtDiffValue,
  notifyStructureChanged,
  reasonText,
  useStructureBadge,
} from "@/components/structure";

type Tab = "pending" | "history" | "databases";

export default function EstructuraPage() {
  return (
    <Suspense fallback={null}>
      <EstructuraInner />
    </Suspense>
  );
}

function scopeOf(c: Pick<StructuralChange, "group_name" | "company_name" | "database_kind">): string {
  if (c.database_kind === "source") return [c.group_name, c.company_name].filter(Boolean).join(" / ");
  return `${c.group_name ?? "—"} (todas sus empresas y agencias)`;
}

function EstructuraInner() {
  const params = useSearchParams();
  const router = useRouter();
  const [tab, setTab] = useState<Tab>("pending");
  const [openChange, setOpenChange] = useState<number | null>(params.get("change") ? Number(params.get("change")) : null);
  const [openDb, setOpenDb] = useState<number | null>(params.get("db") ? Number(params.get("db")) : null);
  const badge = useStructureBadge();

  useEffect(() => {
    if (params.get("change")) setOpenChange(Number(params.get("change")));
    if (params.get("db")) setOpenDb(Number(params.get("db")));
  }, [params]);

  const closeChange = () => {
    setOpenChange(null);
    if (params.get("change")) router.replace(withoutParam(params, "change"));
  };
  const closeDb = () => {
    setOpenDb(null);
    if (params.get("db")) router.replace(withoutParam(params, "db"));
  };

  const TABS: { key: Tab; label: string; count?: number }[] = [
    { key: "pending", label: "Cambios pendientes", count: badge?.pending_changes },
    { key: "history", label: "Historial" },
    { key: "databases", label: "Bases monitoreadas", count: (badge?.awaiting_baseline ?? 0) + (badge?.unverifiable_databases ?? 0) },
  ];

  return (
    <>
      <PageHeader
        title="Estructura"
        description={`Inventario estructural (tablas, vistas, columnas, llaves e índices) que hace el agente local en modo solo lectura. Horas en ${tzLabel()}.`}
      />
      <p className="mb-4 flex items-start gap-2 text-sm text-slate-500">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        <span>
          Se compara contra una <b>línea base aprobada</b>. <b>Dar por entendido</b> incorpora a la línea base <b>solo esa diferencia</b>; el
          responsable elegido es una <b>atribución manual</b>, no una prueba de autoría. Es una comparación periódica: un objeto creado y
          eliminado entre dos inventarios no se detecta. Estas alertas son independientes de las incidencias de carga.
        </span>
      </p>

      <div className="mb-4 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={cx(
              "-mb-px flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-medium transition-colors",
              tab === t.key ? "border-brand-600 text-brand-700" : "border-transparent text-slate-500 hover:text-slate-800",
            )}
          >
            {t.label}
            {t.count ? (
              <span className="inline-flex min-w-[1.25rem] items-center justify-center rounded-full bg-amber-100 px-1.5 text-[11px] font-semibold text-amber-800">
                {t.count}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      {tab === "databases" ? (
        <DatabasesTab onOpen={setOpenDb} />
      ) : (
        <ChangesTab key={tab} view={tab} onOpen={setOpenChange} />
      )}

      {openChange !== null && (
        <ChangeDrawer
          id={openChange}
          onClose={closeChange}
          onOpenOther={(id) => setOpenChange(id)}
          onChanged={notifyStructureChanged}
        />
      )}
      {openDb !== null && <DatabaseDrawer id={openDb} onClose={closeDb} onChanged={notifyStructureChanged} />}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Cambios (pendientes / historial)
// ─────────────────────────────────────────────────────────────────────────────
function ChangesTab({ view, onOpen }: { view: "pending" | "history"; onOpen: (id: number) => void }) {
  const [filter, setFilter, clearFilter] = useUrlFilters(CHANGE_FILTERS);
  const [changeType, setChangeType] = useState("");
  const [attribution, setAttribution] = useState("");
  const [status, setStatus] = useState("");
  const [schema, setSchema] = useState("");
  const [object, setObject] = useState("");

  const path = `admin/structural-changes${qs({
    view,
    group_id: filter.group_id,
    company_id: filter.company_id,
    agency_id: filter.agency_id,
    monitored_database_id: filter.database_id,
    change_type: changeType,
    attribution: view === "history" ? attribution : "",
    status: view === "history" ? status : "",
    schema: schema.trim(),
    object: object.trim(),
    ...dateRange(filter.since, filter.until),
    limit: 500,
  })}`;
  const { data, loading, error, reload } = useApi<{ items: StructuralChange[] }>(path);
  useAutoRefresh(reload);
  const items = data?.items ?? [];

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <FilterBar values={filter} onChange={setFilter} onClear={clearFilter} fields={[...CHANGE_FILTERS]} />
        <Select aria-label="Tipo de cambio" className="w-full sm:w-52" value={changeType} onChange={(e) => setChangeType(e.target.value)}>
          <option value="">Todo tipo de cambio</option>
          {Object.entries(CHANGE_TYPE_LABEL).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </Select>
        {view === "history" && (
          <>
            <Select aria-label="Estado" className="w-full sm:w-40" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Todo estado</option>
              <option value="acknowledged">Entendido</option>
              <option value="superseded">Reemplazado</option>
              <option value="reverted">Revertido</option>
              <option value="out_of_scope">Fuera de alcance</option>
            </Select>
            <Select aria-label="Responsable" className="w-full sm:w-48" value={attribution} onChange={(e) => setAttribution(e.target.value)}>
              <option value="">Todo responsable</option>
              <option value="client">{ATTRIBUTION.client}</option>
              <option value="nexus">{ATTRIBUTION.nexus}</option>
              <option value="none">Sin atribuir</option>
            </Select>
          </>
        )}
        <Input aria-label="Esquema" placeholder="Esquema" className="w-full sm:w-32" value={schema} onChange={(e) => setSchema(e.target.value)} />
        <Input aria-label="Objeto" placeholder="Objeto (contiene)" className="w-full sm:w-44" value={object} onChange={(e) => setObject(e.target.value)} />
        <Button variant="secondary" size="sm" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={() => void reload()} loading={loading && Boolean(data)}>
          Actualizar
        </Button>
        {data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(items.length)} cambio(s)</span>}
      </div>

      <Card>
        <DataState
          loading={loading}
          error={error}
          hasData={Boolean(data)}
          empty={items.length === 0}
          onRetry={reload}
          emptyTitle={view === "pending" ? "Sin cambios estructurales pendientes" : "Sin historial con estos filtros"}
          emptyDescription={view === "pending" ? "La estructura observada coincide con la línea base aprobada." : undefined}
        >
          <Table>
            <thead>
              <tr>
                <Th>Objeto</Th>
                <Th>Cambio</Th>
                <Th>Base / alcance</Th>
                <Th>Primera detección / última observación</Th>
                <Th>Estado</Th>
                <Th>Evidencia</Th>
                <Th className="text-right">Detalle</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((c) => (
                <Tr key={c.id}>
                  <Td>
                    <p className="font-mono text-[13px] font-medium text-slate-900">
                      {c.schema_name}.{c.object_name}
                    </p>
                    <p className="text-xs text-slate-500">
                      #{c.id} · {OBJECT_TYPE[c.object_type] ?? c.object_type}
                    </p>
                  </Td>
                  <Td>
                    <ChangeKindBadge kind={c.change_kind} />
                    <div className="mt-1 flex max-w-xs flex-wrap gap-1">
                      {c.change_types
                        .filter((t) => t !== c.change_kind && t !== "object_added" && t !== "object_removed")
                        .map((t) => (
                          <span key={t} className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
                            {CHANGE_TYPE_LABEL[t] ?? t}
                          </span>
                        ))}
                    </div>
                  </Td>
                  <Td className="text-xs text-slate-600">
                    <p className="font-medium text-slate-700">{c.database_name}</p>
                    <p>{scopeOf(c)}</p>
                  </Td>
                  <Td className="whitespace-nowrap text-xs">
                    <p>{fmtDateTz(c.first_detected_at)}</p>
                    <p className="text-slate-500">
                      {fmtDateTz(c.last_observed_at)} · ×{c.observation_count}
                    </p>
                  </Td>
                  <Td>
                    <ChangeStatusBadge status={c.status} />
                    {c.attribution && <p className="mt-1 text-[11px] text-slate-600">{ATTRIBUTION[c.attribution]}</p>}
                    {c.ticket_ref && <p className="text-[11px] text-slate-400">Ticket {c.ticket_ref}</p>}
                  </Td>
                  <Td className="text-xs">
                    {c.evidence_count > 0 ? (
                      <span title="Evidencia técnica (no prueba autoría)">
                        <Badge tone="blue">{c.evidence_count} técnica</Badge>
                      </span>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </Td>
                  <Td className="text-right">
                    <IconButton label="Ver detalle" onClick={() => onOpen(c.id)}>
                      <Eye className="h-4 w-4" />
                    </IconButton>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Detalle de un cambio + "Dar por entendido" + reclasificar
// ─────────────────────────────────────────────────────────────────────────────
function ChangeDrawer({
  id,
  onClose,
  onOpenOther,
  onChanged,
}: {
  id: number;
  onClose: () => void;
  onOpenOther: (id: number) => void;
  onChanged: () => void;
}) {
  const { data, loading, error, reload } = useApi<StructuralChangeDetail>(`admin/structural-changes/${id}`);
  const toast = useToast();
  const [attribution, setAttribution] = useState<Attribution | "">("");
  const [comment, setComment] = useState("");
  const [ticket, setTicket] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [defs, setDefs] = useState<{ previous: string | null; current: string | null } | null>(null);

  useEffect(() => {
    setAttribution("");
    setComment("");
    setTicket("");
    setReason("");
    setConflict(null);
    setDefs(null);
  }, [id]);

  const c = data;

  const submitAck = async () => {
    if (!c || !attribution) return;
    setBusy("ack");
    setConflict(null);
    try {
      await api(`admin/structural-changes/${c.id}/acknowledge`, {
        method: "POST",
        body: {
          attribution,
          comment: comment.trim() || null,
          ticket_ref: ticket.trim() || null,
          expected_version: c.row_version,
          expected_observed_fingerprint: c.observed_fingerprint,
        },
      });
      toast.success("Cambio dado por entendido: se incorporó solo esta diferencia a la línea base.");
      setAttribution("");
      setComment("");
      setTicket("");
      onChanged();
      void reload();
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 409) {
        setConflict(err.message);
        void reload();
      } else toast.error(err.message);
    } finally {
      setBusy(null);
    }
  };

  const submitReclassify = async () => {
    if (!c || !attribution || reason.trim().length < 5) return;
    setBusy("reclassify");
    try {
      await api(`admin/structural-changes/${c.id}/reclassify`, {
        method: "POST",
        body: { attribution, reason: reason.trim(), ticket_ref: ticket.trim() || null, expected_version: c.row_version },
      });
      toast.success("Responsable reclasificado (se conservan los valores anteriores en el historial).");
      setReason("");
      onChanged();
      void reload();
    } catch (e) {
      toast.error((e as Error).message);
      void reload();
    } finally {
      setBusy(null);
    }
  };

  const loadDefinitions = async () => {
    if (!c) return;
    setBusy("defs");
    try {
      setDefs(await api<{ previous: string | null; current: string | null }>(`admin/structural-changes/${c.id}/definitions`));
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Modal open onClose={onClose} title={c ? `Cambio estructural #${c.id}` : "Cambio estructural"} description={c ? `${c.schema_name}.${c.object_name}` : undefined} size="xl">
      {loading && !c ? (
        <p className="py-10 text-center text-sm text-slate-500">Cargando…</p>
      ) : error && !c ? (
        <p className="py-10 text-center text-sm text-red-600">{error}</p>
      ) : c ? (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-2">
            <ChangeKindBadge kind={c.change_kind} />
            <ChangeStatusBadge status={c.status} />
            <Badge tone="slate">{OBJECT_TYPE[c.object_type] ?? c.object_type}</Badge>
            {c.attribution && <Badge tone="blue">{ATTRIBUTION[c.attribution]}</Badge>}
          </div>

          {(c.database_state.verification_status === "unverifiable" || c.database_state.verification_status === "never") && (
            <p className="flex items-start gap-2 rounded-md bg-red-50 p-3 text-xs text-red-800">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              No se pudo verificar la estructura en el último intento; se muestra la última comparación confiable
              {c.database_state.last_verified_at ? ` (${fmtDateTz(c.database_state.last_verified_at)})` : ""}.
            </p>
          )}

          {c.status === "superseded" && c.superseded_by_id && (
            <p className="rounded-md bg-amber-50 p-3 text-xs text-amber-800">
              El objeto volvió a cambiar antes de revisarlo. Esta versión ya no se puede aceptar;{" "}
              <button type="button" className="font-semibold underline" onClick={() => onOpenOther(c.superseded_by_id!)}>
                revise la alerta #{c.superseded_by_id}
              </button>
              .
            </p>
          )}

          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
            <Item label="Base de datos" value={`${c.database_name} (${c.database_kind === "dwh" ? "DWH" : "origen"})`} />
            <Item label="Grupo / empresa" value={scopeOf(c)} />
            <Item label="Esquema · objeto · tipo" value={`${c.schema_name} · ${c.object_name} · ${OBJECT_TYPE[c.object_type]}`} mono />
            <Item label="Primera detección" value={fmtDateTz(c.first_detected_at)} />
            <Item label="Última observación" value={`${fmtDateTz(c.last_observed_at)} (${fmtAgo(c.last_observed_at)}) · ${c.observation_count} vez/veces`} />
            <Item label="Versión de línea base comparada" value={`v${c.baseline_version}`} />
            {c.status === "acknowledged" && (
              <>
                <Item label="Responsable (atribución manual)" value={c.attribution ? ATTRIBUTION[c.attribution] : "—"} />
                <Item label="Entendido por / fecha" value={`${c.ack_by ?? "—"} · ${fmtDateTz(c.ack_at)}`} />
                <Item label="Ticket" value={c.ticket_ref ?? "—"} />
                {c.ack_comment && <Item label="Comentario" value={c.ack_comment} />}
                {c.reclassified_at && <Item label="Reclasificado" value={`${c.reclassified_by ?? "—"} · ${fmtDateTz(c.reclassified_at)}`} />}
              </>
            )}
          </dl>

          <DiffSection c={c} />

          {(c.previous_definition_hash || c.current_definition_hash) && (
            <div className="rounded-md border border-slate-200 p-3 text-xs text-slate-600">
              <p className="mb-1 flex items-center gap-1.5 font-medium text-slate-700">
                <Lock className="h-3.5 w-3.5" /> Definición SQL de la vista (protegida)
              </p>
              <p>
                Huella anterior <span className="font-mono">{c.previous_definition_hash?.slice(0, 12) ?? "—"}</span> → actual{" "}
                <span className="font-mono">{c.current_definition_hash?.slice(0, 12) ?? "—"}</span>. El SQL no se muestra en el detalle general.
              </p>
              {c.definitions_stored && c.definitions_viewable ? (
                defs ? (
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-[11px]">{defs.previous ?? "—"}</pre>
                    <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-[11px]">{defs.current ?? "—"}</pre>
                  </div>
                ) : (
                  <Button className="mt-2" size="sm" variant="secondary" icon={<Eye className="h-3.5 w-3.5" />} loading={busy === "defs"} onClick={() => void loadDefinitions()}>
                    Ver SQL (acceso sensible, queda registrado)
                  </Button>
                )
              ) : (
                <p className="mt-1 text-slate-500">
                  {c.definitions_stored
                    ? "Ver el SQL requiere el permiso inventory.view_definitions."
                    : "El texto de la definición no se guarda para esta base (solo su huella)."}
                </p>
              )}
            </div>
          )}

          <div className="rounded-md border border-sky-200 bg-sky-50/50 p-3">
            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-sky-800">Evidencia técnica (no prueba autoría)</p>
            {c.evidence.length === 0 ? (
              <p className="text-xs text-slate-500">Sin evidencia técnica asociada. La autoría real requiere auditoría del motor, que no está habilitada.</p>
            ) : (
              <ul className="space-y-1 text-xs text-slate-700">
                {c.evidence.map((e, i) => (
                  <li key={i}>
                    {e.type === "nexus_execution" ? (
                      <>
                        Ejecución Nexus <span className="font-mono">{e.execution_id?.slice(0, 8)}</span> (tarea #{e.task_id}, {fmtDateTz(e.finished_at)}):{" "}
                        <b>{e.action === "add_column" ? `agregó columna(s) ${(e.columns ?? []).join(", ")}` : e.action === "create_table" ? "creó la tabla" : "aplicó DDL del catálogo"}</b>
                      </>
                    ) : (
                      <>
                        Coincide con el catálogo Nexus: <b>{e.object_name}</b> (objeto #{e.object_catalog_id})
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Historial</p>
            <ol className="space-y-1.5 border-l border-slate-200 pl-4">
              {c.events.map((e) => (
                <li key={e.id} className="text-xs">
                  <span className="whitespace-nowrap text-slate-500">{fmtDateTz(e.created_at)}</span> <b className="text-slate-800">{CHANGE_EVENT_LABEL[e.event_type] ?? e.event_type}</b>
                  {e.actor !== "system" && <span className="text-slate-500"> · {e.actor}</span>}
                  {e.message && <span className="text-slate-600"> — {e.message}</span>}
                  {e.event_type === "reclassified" && <ReclassifiedDetail data={e.data} />}
                </li>
              ))}
            </ol>
          </div>

          {c.object_history.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Otras alertas del mismo objeto</p>
              <ul className="flex flex-wrap gap-2 text-xs">
                {c.object_history.map((h) => (
                  <li key={h.id}>
                    <button type="button" onClick={() => onOpenOther(h.id)} className="rounded border border-slate-200 px-2 py-1 hover:bg-slate-50">
                      #{h.id} · {CHANGE_TYPE_LABEL[h.change_kind]} · <ChangeStatusBadge status={h.status} />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {conflict && (
            <p className="rounded-md bg-amber-50 p-3 text-xs text-amber-800">
              <b>No se aplicó:</b> {conflict}
            </p>
          )}

          {(c.status === "pending" || c.status === "acknowledged") &&
            !(c.status === "pending" ? c.allowed_actions?.acknowledge : c.allowed_actions?.reclassify) && (
              <p className="flex items-center gap-2 border-t border-slate-200 pt-4 text-xs text-slate-500">
                <Lock className="h-3.5 w-3.5" />
                Sin permiso para {c.status === "pending" ? "dar por entendido" : "reclasificar"} cambios de esta base (se requiere en todos sus grupos).
              </p>
            )}

          {c.status === "pending" && c.allowed_actions?.acknowledge && (
            <div className="space-y-3 border-t border-slate-200 pt-4">
              <p className="text-sm font-semibold text-slate-900">Dar por entendido</p>
              <AttributionPicker value={attribution} onChange={setAttribution} name="ack-attr" />
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Comentario" htmlFor="ack-comment">
                  <Textarea id="ack-comment" rows={2} maxLength={1000} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Opcional" />
                </Field>
                <Field label="Referencia de ticket" htmlFor="ack-ticket">
                  <Input id="ack-ticket" maxLength={100} value={ticket} onChange={(e) => setTicket(e.target.value)} placeholder="Opcional (p. ej. TCK-123)" />
                </Field>
              </div>
              <p className="text-xs text-slate-500">
                Se incorpora a la línea base <b>solo esta diferencia</b> (versión mostrada, huella <span className="font-mono">{c.observed_fingerprint.slice(0, 12)}</span>). Si el
                objeto cambió otra vez no se acepta en silencio. No resuelve incidencias de carga. Se registra el usuario y la fecha.
              </p>
              <Button icon={<CheckCheck className="h-4 w-4" />} disabled={!attribution} loading={busy === "ack"} onClick={() => void submitAck()}>
                Dar por entendido
              </Button>
            </div>
          )}

          {c.status === "acknowledged" && c.allowed_actions?.reclassify && (
            <div className="space-y-3 border-t border-slate-200 pt-4">
              <p className="text-sm font-semibold text-slate-900">Reclasificar responsable</p>
              <AttributionPicker value={attribution} onChange={setAttribution} name="rec-attr" />
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Motivo" htmlFor="rec-reason" required hint="Obligatorio. Se conservan los valores anteriores, el usuario y la fecha.">
                  <Textarea id="rec-reason" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
                </Field>
                <Field label="Referencia de ticket" htmlFor="rec-ticket">
                  <Input id="rec-ticket" maxLength={100} value={ticket} onChange={(e) => setTicket(e.target.value)} placeholder={c.ticket_ref ?? "Opcional"} />
                </Field>
              </div>
              <Button
                variant="secondary"
                icon={<RotateCcw className="h-4 w-4" />}
                disabled={!attribution || reason.trim().length < 5}
                loading={busy === "reclassify"}
                onClick={() => void submitReclassify()}
              >
                Reclasificar
              </Button>
            </div>
          )}
        </div>
      ) : null}
    </Modal>
  );
}

function ReclassifiedDetail({ data }: { data: Record<string, unknown> }) {
  const prev = (data.previous ?? {}) as { attribution?: Attribution; ticket_ref?: string | null };
  const next = (data.new ?? {}) as { attribution?: Attribution; ticket_ref?: string | null };
  return (
    <span className="block text-slate-500">
      {prev.attribution ? ATTRIBUTION[prev.attribution] : "—"}
      {prev.ticket_ref ? ` (${prev.ticket_ref})` : ""} → {next.attribution ? ATTRIBUTION[next.attribution] : "—"}
      {next.ticket_ref ? ` (${next.ticket_ref})` : ""}
    </span>
  );
}

function AttributionPicker({ value, onChange, name }: { value: Attribution | ""; onChange: (v: Attribution) => void; name: string }) {
  return (
    <fieldset>
      <legend className="mb-1 text-sm font-medium text-slate-700">
        Responsable <span className="text-red-500">*</span>
      </legend>
      <div className="flex flex-wrap gap-2">
        {(Object.keys(ATTRIBUTION) as Attribution[]).map((k) => (
          <label
            key={k}
            className={cx(
              "flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm",
              value === k ? "border-brand-500 bg-brand-50 text-brand-800" : "border-slate-300 text-slate-700 hover:bg-slate-50",
            )}
          >
            <input type="radio" name={name} value={k} checked={value === k} onChange={() => onChange(k)} className="accent-brand-600" />
            {ATTRIBUTION[k]}
          </label>
        ))}
      </div>
      <p className="mt-1 text-xs text-slate-500">Atribución manual; no es prueba de autoría (esa requiere auditoría del motor).</p>
    </fieldset>
  );
}

function DiffSection({ c }: { c: StructuralChangeDetail }) {
  if (c.change_kind !== "object_modified") {
    const s = c.change_kind === "object_added" ? c.current_structure : c.previous_structure;
    return (
      <div>
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
          {c.change_kind === "object_added" ? "Estructura actual (objeto nuevo)" : "Estructura anterior (objeto eliminado)"}
        </p>
        <StructureView s={s} />
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Anterior vs. actual</p>
      <div className="overflow-x-auto rounded-md border border-slate-200">
        <table className="min-w-full text-xs">
          <thead className="bg-slate-50 text-left text-slate-500">
            <tr>
              <th className="px-3 py-2">Cambio</th>
              <th className="px-3 py-2">Elemento</th>
              <th className="px-3 py-2">Anterior</th>
              <th className="px-3 py-2">Actual</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {c.diffs.map((d, i) => (
              <tr key={i}>
                <td className="whitespace-nowrap px-3 py-1.5 font-medium text-slate-700">{CHANGE_TYPE_LABEL[d.kind] ?? d.kind}</td>
                <td className="px-3 py-1.5 font-mono">{d.item ?? "—"}</td>
                <td className="px-3 py-1.5 font-mono text-red-700">{fmtDiffValue(d.before)}</td>
                <td className="px-3 py-1.5 font-mono text-emerald-700">{fmtDiffValue(d.after)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-slate-500 hover:text-slate-700">Ver estructura completa anterior y actual</summary>
        <div className="mt-2 grid gap-3 lg:grid-cols-2">
          <div>
            <p className="mb-1 font-medium text-slate-600">Anterior (línea base)</p>
            <StructureView s={c.previous_structure} />
          </div>
          <div>
            <p className="mb-1 font-medium text-slate-600">Actual (observada)</p>
            <StructureView s={c.current_structure} />
          </div>
        </div>
      </details>
    </div>
  );
}

function StructureView({ s }: { s: ObjectStructure | null }) {
  if (!s) return <p className="text-xs text-slate-400">—</p>;
  const cols = Object.entries(s.columns ?? {}).sort(([a], [b]) => a.localeCompare(b));
  const cons = Object.entries(s.constraints ?? {});
  const idx = Object.entries(s.indexes ?? {});
  return (
    <div className="space-y-2 text-xs">
      <div className="overflow-x-auto rounded-md border border-slate-200">
        <table className="min-w-full">
          <thead className="bg-slate-50 text-left text-slate-500">
            <tr>
              <th className="px-2 py-1.5">Columna</th>
              <th className="px-2 py-1.5">Tipo</th>
              <th className="px-2 py-1.5">Nulos</th>
              <th className="px-2 py-1.5">Defecto</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 font-mono">
            {cols.map(([n, col]) => (
              <tr key={n}>
                <td className="px-2 py-1">{n}</td>
                <td className="px-2 py-1">{col.type}</td>
                <td className="px-2 py-1">{col.not_null ? "NOT NULL" : "NULL"}</td>
                <td className="px-2 py-1">{col.default ?? (col.generated ? `GENERATED ${String(col.generated)}` : "—")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cons.length > 0 && (
        <ul className="space-y-0.5 font-mono text-[11px] text-slate-600">
          {cons.map(([n, k]) => (
            <li key={n}>
              <span className="text-slate-400">restricción</span> {n}: {k.definition}
            </li>
          ))}
        </ul>
      )}
      {idx.length > 0 && (
        <ul className="space-y-0.5 font-mono text-[11px] text-slate-600">
          {idx.map(([n, k]) => (
            <li key={n}>
              <span className="text-slate-400">índice</span> {n}: {k.unique ? "UNIQUE " : ""}
              {k.definition}
            </li>
          ))}
        </ul>
      )}
      {s.definition_hash && (
        <p className="text-[11px] text-slate-500">
          Definición (huella): <span className="font-mono">{String(s.definition_hash).slice(0, 12)}</span>
        </p>
      )}
    </div>
  );
}

function Item({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className={cx("text-slate-800", mono && "font-mono text-xs")}>{value}</dd>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Bases monitoreadas
// ─────────────────────────────────────────────────────────────────────────────
function DatabasesTab({ onOpen }: { onOpen: (id: number) => void }) {
  const [filter, setFilter, clearFilter] = useUrlFilters(DB_FILTERS);
  const { canAny } = useSession();
  const { data, loading, error, reload } = useApi<{ items: MonitoredDatabase[] }>(
    `admin/monitored-databases${qs({ group_id: filter.group_id, company_id: filter.company_id, agency_id: filter.agency_id })}`,
  );
  useAutoRefresh(reload);
  const [creating, setCreating] = useState(false);
  const items = data?.items ?? [];

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <FilterBar values={filter} onChange={setFilter} onClear={clearFilter} fields={[...DB_FILTERS]} />
        <Button variant="secondary" size="sm" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={() => void reload()} loading={loading && Boolean(data)}>
          Actualizar
        </Button>
        {canAny("inventory.configure") && (
          <Button size="sm" icon={<Plus className="h-3.5 w-3.5" />} className="sm:ml-auto" onClick={() => setCreating(true)}>
            Monitorear origen (opcional)
          </Button>
        )}
      </div>
      <p className="mb-3 text-xs text-slate-500">
        El DWH de cada grupo se registra solo cuando un agente lo alcanza (una sola base aunque la compartan varias agencias; la inventaría una
        instalación a la vez). El origen (DMS) solo se inventaría si se habilita explícitamente aquí.
      </p>
      <Card>
        <DataState
          loading={loading}
          error={error}
          hasData={Boolean(data)}
          empty={items.length === 0}
          onRetry={reload}
          emptyTitle="Sin bases monitoreadas"
          emptyDescription="Aparecerán cuando un agente v5.1+ contacte a Nexus."
        >
          <Table>
            <thead>
              <tr>
                <Th>Base</Th>
                <Th>Alcance</Th>
                <Th>Estado</Th>
                <Th>Línea base</Th>
                <Th>Última verificación</Th>
                <Th>Responsable del inventario</Th>
                <Th className="text-right">Pendientes</Th>
                <Th className="text-right">Detalle</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((d) => (
                <Tr key={d.id}>
                  <Td>
                    <p className="flex items-center gap-1.5 font-medium text-slate-900">
                      <Database className="h-3.5 w-3.5 text-slate-400" /> {d.display_name}
                    </p>
                    <p className="text-xs text-slate-500">
                      #{d.id} · {d.kind === "dwh" ? "DWH" : "Origen"} · {d.engine}
                    </p>
                  </Td>
                  <Td className="text-xs text-slate-600">
                    {d.links.length ? d.links.map((l) => [l.group_name, l.company_name].filter(Boolean).join(" / ")).join("; ") : d.group_name ?? "—"}
                  </Td>
                  <Td>
                    <EffectiveStatusBadge status={d.effective_status} />
                    {d.last_reason_code && d.effective_status !== "verified" && <p className="mt-1 text-[11px] text-slate-500">{reasonText(d.last_reason_code)}</p>}
                    {d.stale && <p className="mt-1 text-[11px] text-red-600">Sin inventario reciente</p>}
                  </Td>
                  <Td className="text-xs">
                    <Badge tone={MONITORED_STATE[d.state].tone}>{MONITORED_STATE[d.state].label}</Badge>
                    <p className="mt-1 text-slate-500">
                      v{d.baseline_version} · {fmtNumber(d.baseline_objects)} obj. aprobados · {fmtNumber(d.observed_objects)} observados
                    </p>
                  </Td>
                  <Td className="whitespace-nowrap text-xs">
                    <p>{fmtDateTz(d.last_verified_at)}</p>
                    <p className="text-slate-500">cada {fmtSeconds(d.scan_interval_seconds)}</p>
                  </Td>
                  <Td className="text-xs">
                    {d.lease_installation_name ? (
                      <>
                        <p>{d.lease_installation_name}</p>
                        <p className={d.lease_active ? "text-slate-500" : "text-amber-600"}>{d.lease_active ? `hasta ${fmtDateTz(d.lease_until)}` : "vencido"}</p>
                      </>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </Td>
                  <Td className="text-right tabular-nums">{d.pending_changes ? <Badge tone="amber">{d.pending_changes}</Badge> : "0"}</Td>
                  <Td className="text-right">
                    <IconButton label="Ver y configurar" onClick={() => onOpen(d.id)}>
                      <Eye className="h-4 w-4" />
                    </IconButton>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>
      {creating && (
        <CreateSourceModal
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            void reload();
            onOpen(id);
          }}
        />
      )}
    </>
  );
}

function splitPatterns(v: string): string[] {
  return v
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function CreateSourceModal({ onClose, onCreated }: { onClose: () => void; onCreated: (id: number) => void }) {
  const companies = useApi<ListResponse<Company>>("admin/companies");
  const { can } = useSession();
  const toast = useToast();
  const [companyId, setCompanyId] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [interval, setIntervalS] = useState("3600");
  const [include, setInclude] = useState("");
  const [exclude, setExclude] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      const r = await api<MonitoredDatabase>("admin/monitored-databases", {
        method: "POST",
        body: {
          kind: "source",
          company_id: Number(companyId),
          enabled,
          scan_interval_seconds: Number(interval),
          schema_include: splitPatterns(include),
          schema_exclude: splitPatterns(exclude),
        },
      });
      if (r.warning) toast.info(r.warning);
      else toast.success("Origen registrado.");
      onCreated(r.id);
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title="Monitorear el origen (DMS)"
      description="Opcional. Solo lectura, limitado a los esquemas indicados y a los permisos del usuario de origen."
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancelar
          </Button>
          <Button disabled={!companyId} loading={busy} onClick={() => void save()}>
            Registrar
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Empresa (origen)" htmlFor="src-company" required>
          <Select id="src-company" value={companyId} onChange={(e) => setCompanyId(e.target.value)}>
            <option value="">Seleccione…</option>
            <CompanyOptions companies={(companies.data?.items ?? []).filter((c) => can("inventory.configure", c.group_id))} />
          </Select>
        </Field>
        <Switch checked={enabled} onChange={setEnabled} label="Habilitar ahora" description="Si queda deshabilitado, ningún agente inventaría el origen." />
        <Field label="Frecuencia (segundos)" htmlFor="src-interval" hint="Independiente de las cargas. Mínimo 60.">
          <Input id="src-interval" type="number" min={60} value={interval} onChange={(e) => setIntervalS(e.target.value)} />
        </Field>
        <Field label="Esquemas incluidos" htmlFor="src-include" hint="Patrones separados por coma (p. ej. dbo, ventas_*). Vacío = todos los esquemas de usuario.">
          <Input id="src-include" value={include} onChange={(e) => setInclude(e.target.value)} />
        </Field>
        <Field label="Esquemas excluidos" htmlFor="src-exclude" hint="Los esquemas del sistema siempre se excluyen.">
          <Input id="src-exclude" value={exclude} onChange={(e) => setExclude(e.target.value)} />
        </Field>
        <p className="text-xs text-slate-500">Hoy el inventario soporta orígenes PostgreSQL; otros motores se reportarán como «No se pudo verificar la estructura».</p>
      </div>
    </Modal>
  );
}

function DatabaseDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { data, loading, error, reload } = useApi<MonitoredDatabaseDetail>(`admin/monitored-databases/${id}`);
  const baseline = useApi<BaselineResponse>(`admin/monitored-databases/${id}/baseline?limit=5000`);
  const toast = useToast();
  const confirm = useConfirm();
  // Permisos calculados por el backend sobre TODOS los grupos de la base (DWH compartido).
  const canConfigure = Boolean(data?.allowed_actions?.configure);
  const canApprove = Boolean(data?.allowed_actions?.approve_baseline);
  const [busy, setBusy] = useState<string | null>(null);
  const [form, setForm] = useState<{ enabled: boolean; interval: string; include: string; exclude: string; viewDefs: boolean } | null>(null);
  const [selected, setSelected] = useState<Set<string> | null>(null);
  const [resetReason, setResetReason] = useState("");
  const [dupReason, setDupReason] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    if (data)
      setForm({
        enabled: data.enabled,
        interval: String(data.scan_interval_seconds),
        include: data.schema_include.join(", "),
        exclude: data.schema_exclude.join(", "),
        viewDefs: data.view_definitions_enabled,
      });
  }, [data]);

  const items = useMemo(() => baseline.data?.items ?? [], [baseline.data]);
  const keyOf = (i: { schema_name: string; name: string; type: string }) => `${i.schema_name}\u0000${i.name}\u0000${i.type}`;
  useEffect(() => {
    if (baseline.data?.view === "proposal") setSelected(new Set(items.map(keyOf)));
  }, [baseline.data, items]);

  const refresh = useCallback(() => {
    void reload();
    void baseline.reload();
    onChanged();
  }, [reload, baseline, onChanged]);

  const act = async (key: string, path: string, body?: unknown, ok?: string) => {
    setBusy(key);
    try {
      await api(path, { method: key === "save" ? "PUT" : "POST", body });
      if (ok) toast.success(ok);
      refresh();
      return true;
    } catch (e) {
      toast.error((e as Error).message);
      if ((e as ApiError).status === 409) refresh();
      return false;
    } finally {
      setBusy(null);
    }
  };

  const resolveDup = async (action: "merge" | "undo") => {
    setBusy(action);
    try {
      const r = await api<{ id: number }>(`admin/monitored-databases/${id}/resolve-duplicate`, { method: "POST", body: { action, reason: dupReason.trim() } });
      toast.success(action === "merge" ? `Fusionada: la base #${r.id} conserva línea base e historial.` : "Detección de duplicado deshecha.");
      setDupReason("");
      onChanged();
      if (action === "merge" && r.id !== id) onClose();
      else refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (!form || !data) return;
    if (data.view_definitions_enabled && !form.viewDefs) {
      const ok = await confirm({
        title: "Dejar de guardar SQL de vistas",
        message: "Se borrará el SQL cifrado ya guardado de esta base (quedan solo las huellas). ¿Continuar?",
        confirmLabel: "Borrar y guardar",
        danger: true,
      });
      if (!ok) return;
    }
    await saveNow();
  };

  const saveNow = () =>
    form &&
    act(
      "save",
      `admin/monitored-databases/${id}`,
      {
        enabled: form.enabled,
        scan_interval_seconds: Number(form.interval),
        schema_include: splitPatterns(form.include),
        schema_exclude: splitPatterns(form.exclude),
        view_definitions_enabled: form.viewDefs,
      },
      "Configuración guardada (queda en el historial de la base).",
    );

  const approve = async () => {
    if (!baseline.data?.snapshot || !selected) return;
    const all = selected.size === items.length;
    const ok = await confirm({
      title: "Aprobar línea base",
      message: all
        ? `Se aprobarán ${items.length} objeto(s) del inventario del ${fmtDateTz(baseline.data.snapshot.received_at)} como referencia. No se asume que los haya creado Nexus.`
        : `Se aprobarán ${selected.size} de ${items.length} objeto(s). Los ${items.length - selected.size} no seleccionados quedarán como cambios pendientes para revisarlos.`,
      confirmLabel: "Aprobar",
    });
    if (!ok) return;
    await act(
      "approve",
      `admin/monitored-databases/${id}/baseline/approve`,
      {
        expected_snapshot_id: baseline.data.snapshot.id,
        object_keys: all ? null : items.filter((i) => selected.has(keyOf(i))).map((i) => ({ schema_name: i.schema_name, name: i.name, type: i.type })),
      },
      "Línea base aprobada.",
    );
  };

  const d = data;
  const snap = baseline.data?.snapshot;
  const filtered = items.filter((i) => !search || `${i.schema_name}.${i.name}`.toLowerCase().includes(search.toLowerCase()));

  return (
    <Modal open onClose={onClose} title={d ? d.display_name : "Base monitoreada"} description={d ? `#${d.id} · ${d.kind === "dwh" ? "DWH" : "Origen"} · ${d.engine}` : undefined} size="xl">
      {loading && !d ? (
        <p className="py-10 text-center text-sm text-slate-500">Cargando…</p>
      ) : error && !d ? (
        <p className="py-10 text-center text-sm text-red-600">{error}</p>
      ) : d && form ? (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-2">
            <EffectiveStatusBadge status={d.effective_status} />
            <Badge tone={MONITORED_STATE[d.state].tone}>{MONITORED_STATE[d.state].label}</Badge>
            {!d.config_current && <Badge tone="amber">La configuración vigente ya no apunta a esta base</Badge>}
          </div>
          {d.effective_status === "unverifiable" && (
            <p className="flex items-start gap-2 rounded-md bg-red-50 p-3 text-xs text-red-800">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              No se pudo verificar la estructura{d.last_reason_code ? ` (${reasonText(d.last_reason_code)})` : d.stale ? " (sin inventario reciente)" : ""}. Se conserva la
              referencia anterior; no se infieren eliminaciones. Última verificación: {fmtDateTz(d.last_verified_at)}.
            </p>
          )}
          {d.duplicate_of_id && (
            <div className="space-y-2 rounded-md bg-slate-100 p-3 text-xs text-slate-700">
              <p>
                Es la misma base física (identidad fuerte del servidor) que la #{d.duplicate_of_id}: no se inventaría dos veces. Si la configuración
                original ya no apunta a esa base y ambas son del <b>mismo grupo</b> (p. ej. se cambió el nombre del host), <b>fusione</b>: la base
                original conserva su línea base e historial y adopta esta configuración. Nunca se fusiona entre grupos distintos. Si en realidad son
                bases distintas, <b>deshaga</b> la detección.
              </p>
              <Textarea aria-label="Motivo" rows={2} maxLength={500} value={dupReason} onChange={(e) => setDupReason(e.target.value)} placeholder="Motivo (obligatorio)" />
              <div className={cx("flex flex-wrap gap-2", !canConfigure && "hidden")}>
                <Button size="sm" disabled={dupReason.trim().length < 5} loading={busy === "merge"} onClick={() => void resolveDup("merge")}>
                  Fusionar con la #{d.duplicate_of_id}
                </Button>
                <Button size="sm" variant="secondary" disabled={dupReason.trim().length < 5} loading={busy === "undo"} onClick={() => void resolveDup("undo")}>
                  Deshacer duplicado
                </Button>
              </div>
            </div>
          )}

          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
            <Item label="Identidad (configuración)" value={d.identity_key} mono />
            <Item label="Identidad del servidor" value={d.engine_identity ? `${d.engine_identity} (${d.engine_identity_strength})` : "—"} mono />
            <Item label="Responsable del inventario" value={d.lease_installation_name ? `${d.lease_installation_name} · ${d.lease_active ? `hasta ${fmtDateTz(d.lease_until)}` : "vencido"}` : "—"} />
            <Item label="Último intento" value={`${fmtDateTz(d.last_attempt_at)} (${fmtAgo(d.last_attempt_at)})`} />
            <Item label="Última verificación" value={fmtDateTz(d.last_verified_at)} />
            <Item label="Línea base" value={`v${d.baseline_version}${d.baseline_approved_at ? ` · aprobada ${fmtDateTz(d.baseline_approved_at)} por ${d.baseline_approved_by}` : ""}`} />
          </dl>

          {!canConfigure && (
            <p className="flex items-center gap-2 text-xs text-slate-500">
              <Lock className="h-3.5 w-3.5" />
              Solo consulta: configurar esta base requiere «Configurar inventario» en todos sus grupos
              {canApprove ? "" : "; aprobar la línea base requiere «Aprobar línea base»"}.
            </p>
          )}
          <div className={cx("flex flex-wrap gap-2", !canConfigure && "hidden")}>
            <Button size="sm" variant="secondary" icon={<Play className="h-3.5 w-3.5" />} loading={busy === "scan"} onClick={() => void act("scan", `admin/monitored-databases/${id}/scan`, undefined, "Inventario solicitado: el agente responsable lo hará en su próximo ciclo.")}>
              Inventariar ahora
            </Button>
            <Button size="sm" variant="secondary" icon={<Unlink className="h-3.5 w-3.5" />} loading={busy === "release"} onClick={() => void act("release", `admin/monitored-databases/${id}/release-lease`, undefined, "Responsable liberado.")}>
              Liberar responsable
            </Button>
          </div>

          {/* Configuración */}
          <fieldset disabled={!canConfigure} className="rounded-md border border-slate-200 p-4">
            <p className="mb-3 text-sm font-semibold text-slate-900">Configuración</p>
            <div className="grid gap-4 sm:grid-cols-2">
              <Switch checked={form.enabled} onChange={(v) => setForm({ ...form, enabled: v })} label="Monitoreo habilitado" description={d.kind === "source" ? "Origen: opcional y explícito." : undefined} />
              <Switch
                checked={form.viewDefs}
                onChange={(v) => setForm({ ...form, viewDefs: v })}
                label="Guardar SQL de vistas (cifrado)"
                description="Por defecto solo se guarda la huella. Verlo requiere el permiso inventory.view_definitions."
              />
              <Field label="Frecuencia (segundos)" htmlFor="db-interval" hint="Independiente de las cargas ETL (mín. 60).">
                <Input id="db-interval" type="number" min={60} value={form.interval} onChange={(e) => setForm({ ...form, interval: e.target.value })} />
              </Field>
              <div />
              <Field label="Esquemas incluidos" htmlFor="db-include" hint="Patrones separados por coma. Vacío = todos los esquemas de usuario autorizados.">
                <Input id="db-include" value={form.include} onChange={(e) => setForm({ ...form, include: e.target.value })} />
              </Field>
              <Field
                label="Esquemas excluidos"
                htmlFor="db-exclude"
                hint={`Siempre excluidos: esquemas del sistema${d.default_schema_exclude.length ? `, ${d.default_schema_exclude.join(", ")}` : ""}.`}
              >
                <Input id="db-exclude" value={form.exclude} onChange={(e) => setForm({ ...form, exclude: e.target.value })} />
              </Field>
            </div>
            {canConfigure && (
              <Button className="mt-3" size="sm" loading={busy === "save"} onClick={() => void save()}>
                Guardar configuración
              </Button>
            )}
          </fieldset>

          {/* Línea base */}
          <div className="rounded-md border border-slate-200 p-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm font-semibold text-slate-900">
                {baseline.data?.view === "proposal" ? "Propuesta de línea base (último inventario)" : "Línea base aprobada"}
                {baseline.data ? <span className="ml-2 text-xs font-normal text-slate-500">{fmtNumber(baseline.data.total)} objeto(s)</span> : null}
              </p>
              <Input aria-label="Buscar objeto" placeholder="Buscar objeto" className="w-48" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
            {snap && baseline.data?.view === "proposal" && (
              <p className="mb-2 text-xs text-slate-500">
                Inventario #{snap.id} del {fmtDateTz(snap.received_at)} ({snap.status}). No se asume que estos objetos los haya creado Nexus; «Catálogo Nexus» es solo una coincidencia
                de nombre.
                {snap.schemas_unverifiable.length > 0 && (
                  <b className="text-amber-700"> Esquemas no verificables: {snap.schemas_unverifiable.map((s) => s.schema_name).join(", ")} (sus objetos no están en la propuesta).</b>
                )}
              </p>
            )}
            {d.state === "awaiting_first_snapshot" && <p className="text-xs text-slate-500">Aún no hay inventario: se mostrará aquí para aprobarlo.</p>}
            {items.length > 0 && (
              <div className="max-h-80 overflow-auto rounded-md border border-slate-200">
                <table className="min-w-full text-xs">
                  <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
                    <tr>
                      {baseline.data?.view === "proposal" && (
                        <th className="px-2 py-1.5">
                          <input
                            type="checkbox"
                            aria-label="Seleccionar todo"
                            checked={selected?.size === items.length}
                            onChange={(e) => setSelected(new Set(e.target.checked ? items.map(keyOf) : []))}
                          />
                        </th>
                      )}
                      <th className="px-2 py-1.5">Objeto</th>
                      <th className="px-2 py-1.5">Tipo</th>
                      <th className="px-2 py-1.5 text-right">Col.</th>
                      <th className="px-2 py-1.5 text-right">Restr.</th>
                      <th className="px-2 py-1.5 text-right">Índ.</th>
                      <th className="px-2 py-1.5">Evidencia</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {filtered.map((i) => {
                      const k = keyOf(i);
                      return (
                        <tr key={k}>
                          {baseline.data?.view === "proposal" && (
                            <td className="px-2 py-1">
                              <input
                                type="checkbox"
                                aria-label={`Aprobar ${i.schema_name}.${i.name}`}
                                checked={selected?.has(k) ?? false}
                                onChange={(e) => {
                                  const n = new Set(selected ?? []);
                                  if (e.target.checked) n.add(k);
                                  else n.delete(k);
                                  setSelected(n);
                                }}
                              />
                            </td>
                          )}
                          <td className="px-2 py-1 font-mono">
                            {i.schema_name}.{i.name}
                          </td>
                          <td className="px-2 py-1">{OBJECT_TYPE[i.type]}</td>
                          <td className="px-2 py-1 text-right tabular-nums">{i.columns}</td>
                          <td className="px-2 py-1 text-right tabular-nums">{i.constraints}</td>
                          <td className="px-2 py-1 text-right tabular-nums">{i.indexes}</td>
                          <td className="px-2 py-1">{i.nexus_catalog_match ? <Badge tone="blue">Catálogo Nexus</Badge> : null}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {baseline.data?.view === "proposal" && d.state === "baseline_pending" && canApprove && (
              <Button className="mt-3" size="sm" icon={<CheckCheck className="h-3.5 w-3.5" />} loading={busy === "approve"} disabled={!selected || selected.size === 0} onClick={() => void approve()}>
                Aprobar línea base ({selected?.size ?? 0})
              </Button>
            )}
            {d.state === "monitoring" && canApprove && (
              <div className="mt-4 border-t border-slate-100 pt-3">
                <Field label="Reiniciar línea base" htmlFor="reset-reason" hint="Cierra las alertas pendientes (quedan en el historial) y pide un inventario nuevo para aprobarlo. Úselo si cambió el servidor.">
                  <Textarea id="reset-reason" rows={2} maxLength={500} value={resetReason} onChange={(e) => setResetReason(e.target.value)} placeholder="Motivo (obligatorio)" />
                </Field>
                <Button
                  className="mt-2"
                  size="sm"
                  variant="danger"
                  icon={<RotateCcw className="h-3.5 w-3.5" />}
                  disabled={resetReason.trim().length < 5}
                  loading={busy === "reset"}
                  onClick={async () => {
                    const ok = await confirm({
                      title: "Reiniciar línea base",
                      message: "Las alertas pendientes se cerrarán como reemplazadas y habrá que aprobar un inventario nuevo. ¿Continuar?",
                      confirmLabel: "Reiniciar",
                      danger: true,
                    });
                    if (ok && (await act("reset", `admin/monitored-databases/${id}/baseline/reset`, { reason: resetReason.trim() }, "Línea base reiniciada."))) setResetReason("");
                  }}
                >
                  Reiniciar línea base
                </Button>
              </div>
            )}
          </div>

          {/* Inventarios y eventos */}
          <div className="grid gap-4 lg:grid-cols-2">
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Últimos inventarios</p>
              <ul className="space-y-1 text-xs">
                {d.snapshots.map((s) => (
                  <li key={s.id} className="flex flex-wrap items-center gap-2">
                    <Badge tone={s.status === "complete" ? "green" : s.status === "partial" ? "amber" : "red"}>
                      {s.status === "complete" ? "Completo" : s.status === "partial" ? "Parcial" : "No confiable"}
                    </Badge>
                    <span>{fmtDateTz(s.received_at)}</span>
                    <span className="text-slate-500">
                      {s.object_count} obj. · {s.installation_name ?? "—"}
                    </span>
                    {s.reason_code && <span className="text-slate-500">· {reasonText(s.reason_code)}</span>}
                  </li>
                ))}
                {d.snapshots.length === 0 && <li className="text-slate-400">Sin inventarios.</li>}
              </ul>
            </div>
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Historial de la base</p>
              <ol className="space-y-1 border-l border-slate-200 pl-3 text-xs">
                {d.events.map((e) => (
                  <li key={e.id}>
                    <span className="text-slate-500">{fmtDateTz(e.created_at)}</span> <b className="text-slate-800">{MDB_EVENT_LABEL[e.event_type] ?? e.event_type}</b>
                    {e.actor !== "system" && <span className="text-slate-500"> · {e.actor}</span>}
                    {e.message && <span className="text-slate-600"> — {e.message}</span>}
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
