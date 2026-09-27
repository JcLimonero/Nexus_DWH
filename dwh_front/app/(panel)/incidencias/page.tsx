"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { CheckCheck, Eye, Info, RefreshCw, ShieldCheck, X } from "lucide-react";
import { api, qs, useApi } from "@/lib/api";
import type { Incident, IncidentCategory, IncidentDetail, InstallationHealth, Severity, TaskHealth } from "@/lib/types";
import { cx, fmtAgo, fmtDateTz, fmtDuration, fmtNumber, tzLabel } from "@/lib/format";
import { Badge, Button, Card, Field, IconButton, PageHeader, Select, Textarea } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { Modal } from "@/components/ui/modal";
import { useToast } from "@/components/ui/feedback";
import { FilterBar, dateRange, useUrlFilters } from "@/components/scope-filters";
import { useSession } from "@/components/session";

const FILTER_KEYS = ["group_id", "company_id", "agency_id", "task_id", "since", "until"] as const;
import {
  CATEGORY_LABEL,
  CONNECTIVITY,
  DELIVERY_STATUS,
  IncidentStatusBadges,
  SEVERITY,
  SeverityBadge,
  TASK_STATE,
  TRANSITION_LABEL,
  fmtSecs,
  notifyIncidentsChanged,
  useAutoRefresh,
} from "@/components/health";

type View = "active" | "acknowledged" | "resolved";
const TABS: { key: View; label: string; hint: string }[] = [
  { key: "active", label: "Activas", hint: "Abiertas y sin reconocer." },
  { key: "acknowledged", label: "Reconocidas", hint: "Abiertas: alguien las revisó, pero la falla sigue." },
  { key: "resolved", label: "Resueltas", hint: "Terminó la condición de falla (con evidencia) o se cerraron con motivo explícito." },
];

const EVENT_LABEL: Record<string, string> = {
  opened: "Abierta",
  recurred: "Recurrencia",
  acknowledged: "Reconocida",
  resolved: "Resuelta",
  notified: "Notificada",
  notification_failed: "Notificación fallida",
  late_evidence_ignored: "Evidencia antigua ignorada",
};

const EXEC_STATUS: Record<string, { label: string; tone: "green" | "red" | "amber" | "blue" }> = {
  success: { label: "OK", tone: "green" },
  failed: { label: "Fallida", tone: "red" },
  interrupted: { label: "Interrumpida", tone: "amber" },
  running: { label: "En curso", tone: "blue" },
};

function scopeText(i: Pick<Incident, "group_name" | "company_name" | "agency_name">) {
  return [i.group_name, i.company_name, i.agency_name].filter(Boolean).join(" / ") || "—";
}

function durationOf(i: Incident): string {
  if (i.status === "resolved") return fmtSecs(i.duration_seconds);
  const s = (Date.now() - new Date(i.opened_at).getTime()) / 1000;
  return `${fmtSecs(s)} (en curso)`;
}

export default function IncidenciasPage() {
  return (
    <Suspense fallback={null}>
      <IncidenciasInner />
    </Suspense>
  );
}

function IncidenciasInner() {
  const params = useSearchParams();
  const router = useRouter();
  const [view, setView] = useState<View>("active");
  const [filter, setFilter, clearFilter] = useUrlFilters(FILTER_KEYS);
  const [category, setCategory] = useState("");
  const [severity, setSeverity] = useState("");
  const installationId = params.get("installation_id") || "";
  const detailId = params.get("id");
  const [openId, setOpenId] = useState<number | null>(detailId ? Number(detailId) : null);

  useEffect(() => {
    if (detailId) setOpenId(Number(detailId));
  }, [detailId]);

  const { data, loading, error, reload } = useApi<{ items: Incident[] }>(
    `admin/incidents${qs({
      view,
      category,
      severity,
      group_id: filter.group_id,
      company_id: filter.company_id,
      agency_id: filter.agency_id,
      task_id: filter.task_id,
      ...dateRange(filter.since, filter.until),
      installation_id: installationId,
      limit: 300,
    })}`,
  );
  useAutoRefresh(reload);
  const items = data?.items ?? [];

  const closeDetail = () => {
    setOpenId(null);
    if (detailId) router.replace("/incidencias" + (installationId ? `?installation_id=${installationId}` : ""));
  };

  return (
    <>
      <PageHeader
        title="Incidencias"
        description={`Fallas agrupadas por instalación, tarea y categoría. Horas en ${tzLabel()}.`}
        actions={
          <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={() => void reload()} loading={loading && Boolean(data)}>
            Actualizar
          </Button>
        }
      />

      <p className="mb-4 flex items-start gap-2 text-sm text-slate-500">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        <span>
          <b>Reconocer</b> solo indica que alguien la revisó: la incidencia sigue <b>abierta</b> hasta que haya evidencia de recuperación
          (latido recibido, carga confirmada, fin de la ejecución…). Un latido cierra la desconexión pero no los errores de las tareas.
        </span>
      </p>

      <div className="mb-4 flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            title={t.hint}
            onClick={() => setView(t.key)}
            className={cx(
              "-mb-px border-b-2 px-4 py-2 text-sm font-medium transition-colors",
              view === t.key ? "border-brand-600 text-brand-700" : "border-transparent text-slate-500 hover:text-slate-800",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <FilterBar values={filter} onChange={setFilter} onClear={clearFilter} fields={[...FILTER_KEYS]} />
        <Select aria-label="Categoría" className="w-full sm:w-56" value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">Todas las categorías</option>
          {Object.entries(CATEGORY_LABEL).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </Select>
        <Select aria-label="Severidad" className="w-full sm:w-40" value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">Toda severidad</option>
          {(Object.keys(SEVERITY) as Severity[]).map((k) => (
            <option key={k} value={k}>
              {SEVERITY[k].label}
            </option>
          ))}
        </Select>
        {installationId && (
          <Button variant="secondary" size="sm" icon={<X className="h-3.5 w-3.5" />} onClick={() => router.replace("/incidencias")}>
            Instalación {installationId.slice(0, 8)}…
          </Button>
        )}
        {data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(items.length)} incidencia(s)</span>}
      </div>

      <Card>
        <DataState
          loading={loading}
          error={error}
          hasData={Boolean(data)}
          empty={items.length === 0}
          onRetry={reload}
          emptyTitle={view === "resolved" ? "Sin incidencias resueltas" : "Sin incidencias abiertas"}
          emptyDescription={view === "active" ? "Todo en orden con estos filtros." : undefined}
        >
          <Table>
            <thead>
              <tr>
                <Th>Incidencia</Th>
                <Th>Severidad</Th>
                <Th>Estado</Th>
                <Th>Alcance</Th>
                <Th>Primera / última ocurrencia</Th>
                <Th className="text-right">Ocurrencias</Th>
                <Th>Duración</Th>
                <Th className="text-right">Detalle</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((i) => (
                <Tr key={i.id}>
                  <Td>
                    <p className="font-medium text-slate-900">{i.title}</p>
                    <p className="text-xs text-slate-500">
                      #{i.id} · {CATEGORY_LABEL[i.category] ?? i.category_label}
                      {i.last_error_code ? <span className="ml-1 font-mono text-red-700">{i.last_error_code}</span> : null}
                    </p>
                  </Td>
                  <Td>
                    <SeverityBadge severity={i.severity} />
                  </Td>
                  <Td>
                    <IncidentStatusBadges incident={i} />
                    {i.status === "resolved" && <p className="mt-1 text-[11px] text-slate-500">{i.resolution_label}</p>}
                  </Td>
                  <Td className="text-xs text-slate-600">
                    <p>{scopeText(i)}</p>
                    {i.installation_name && <p className="text-[11px] text-slate-400">Instalación: {i.installation_name}</p>}
                  </Td>
                  <Td className="whitespace-nowrap text-xs">
                    <p>{fmtDateTz(i.opened_at)}</p>
                    <p className="text-slate-500">{fmtDateTz(i.last_seen_at)}</p>
                  </Td>
                  <Td className="text-right tabular-nums">{fmtNumber(i.occurrences)}</Td>
                  <Td className="whitespace-nowrap text-xs">{durationOf(i)}</Td>
                  <Td className="text-right">
                    <IconButton label="Ver detalle" onClick={() => setOpenId(i.id)}>
                      <Eye className="h-4 w-4" />
                    </IconButton>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>

      {openId !== null && (
        <IncidentDrawer
          id={openId}
          onClose={closeDetail}
          onChanged={() => {
            void reload();
            notifyIncidentsChanged();
          }}
        />
      )}
    </>
  );
}

function IncidentDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { data, loading, error, reload } = useApi<IncidentDetail>(`admin/incidents/${id}`);
  const toast = useToast();
  const { can } = useSession();
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const act = useCallback(
    async (key: string, path: string, body: unknown, ok: string) => {
      setBusy(key);
      try {
        await api(path, { method: "PUT", body });
        toast.success(ok);
        setComment("");
        setReason("");
        void reload();
        onChanged();
      } catch (e) {
        toast.error((e as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [toast, reload, onChanged],
  );

  const i = data;
  const health = i?.current_health ?? null;
  const isTask = i?.task_id !== null && i?.task_id !== undefined;

  return (
    <Modal open onClose={onClose} title={i ? `Incidencia #${i.id}` : "Incidencia"} description={i?.title} size="xl">
      {loading && !i ? (
        <p className="py-10 text-center text-sm text-slate-500">Cargando…</p>
      ) : error && !i ? (
        <p className="py-10 text-center text-sm text-red-600">{error}</p>
      ) : i ? (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-2">
            <SeverityBadge severity={i.severity} />
            <IncidentStatusBadges incident={i} />
            <Badge tone="slate">{CATEGORY_LABEL[i.category as IncidentCategory] ?? i.category_label}</Badge>
          </div>

          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <Item label="Alcance" value={scopeText(i)} />
            <Item label="Instalación" value={i.installation_name ?? "—"} />
            <Item label="Tarea" value={i.task_id ? `#${i.task_id} · ${i.object_name ?? ""}` : "—"} />
            <Item label="Ocurrencias" value={fmtNumber(i.occurrences)} />
            <Item label="Primera ocurrencia" value={fmtDateTz(i.opened_at)} />
            <Item label="Última ocurrencia" value={`${fmtDateTz(i.last_seen_at)} (${fmtAgo(i.last_seen_at)})`} />
            <Item label="Duración" value={durationOf(i)} />
            <Item label="Último código de error" value={i.last_error_code ?? "—"} mono />
            {i.status === "resolved" && (
              <>
                <Item label="Resuelta" value={`${fmtDateTz(i.resolved_at)} · ${i.resolution_label ?? ""}`} />
                <Item label="Resuelta por" value={`${i.resolved_by ?? "—"}${i.resolution_comment ? ` · ${i.resolution_comment}` : ""}`} />
              </>
            )}
            {i.acknowledged && (
              <Item label="Reconocida" value={`${fmtDateTz(i.acknowledged_at)} por ${i.acknowledged_by ?? "—"}${i.ack_comment ? ` · «${i.ack_comment}»` : ""}`} />
            )}
            <Item label="Notificaciones enviadas" value={`${i.notify_count}${i.last_notified_at ? ` · última ${fmtDateTz(i.last_notified_at)}` : ""}`} />
          </dl>

          {i.last_message_sanitized && (
            <div>
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Último mensaje (saneado)</p>
              <p className="rounded-md bg-slate-50 p-3 font-mono text-xs text-slate-700">{i.last_message_sanitized}</p>
            </div>
          )}

          {/* Estado técnico actual: visible aunque la incidencia esté reconocida */}
          {health && (
            <div className="rounded-md border border-slate-200 p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Estado técnico actual</p>
              {isTask ? (
                <TaskHealthBrief h={health as TaskHealth} />
              ) : (
                <InstallationHealthBrief h={health as InstallationHealth} />
              )}
            </div>
          )}

          {i.executions.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Ejecuciones relacionadas</p>
              <div className="overflow-x-auto rounded-md border border-slate-200">
                <table className="min-w-full text-xs">
                  <thead className="bg-slate-50 text-left text-slate-500">
                    <tr>
                      <th className="px-3 py-2">Inicio</th>
                      <th className="px-3 py-2">Estado</th>
                      <th className="px-3 py-2">Duración</th>
                      <th className="px-3 py-2 text-right">Filas</th>
                      <th className="px-3 py-2">Error</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {i.executions.map((e) => (
                      <tr key={e.execution_id} className={e.execution_id === i.resolved_execution_id ? "bg-emerald-50" : undefined}>
                        <td className="whitespace-nowrap px-3 py-1.5">{fmtDateTz(e.started_at)}</td>
                        <td className="px-3 py-1.5">
                          <Badge tone={EXEC_STATUS[e.status]?.tone ?? "slate"}>{EXEC_STATUS[e.status]?.label ?? e.status}</Badge>
                        </td>
                        <td className="whitespace-nowrap px-3 py-1.5">{fmtDuration(e.duration_ms)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{e.rows_loaded ?? "—"}</td>
                        <td className="px-3 py-1.5 font-mono text-red-700">{e.error_code ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Historial</p>
            <ol className="space-y-1.5 border-l border-slate-200 pl-4">
              {i.events.map((e) => (
                <li key={e.id} className="text-xs">
                  <span className="whitespace-nowrap text-slate-500">{fmtDateTz(e.created_at)}</span>{" "}
                  <b className="text-slate-800">{EVENT_LABEL[e.event_type] ?? e.event_type}</b>
                  {e.actor !== "system" && <span className="text-slate-500"> · {e.actor}</span>}
                  {e.message && <span className="text-slate-600"> — {e.message}</span>}
                </li>
              ))}
            </ol>
          </div>

          {i.deliveries.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">Notificaciones</p>
              <ul className="space-y-1 text-xs">
                {i.deliveries.map((d) => (
                  <li key={d.id} className="flex flex-wrap items-center gap-2">
                    <Badge tone={DELIVERY_STATUS[d.status]?.tone ?? "slate"}>{DELIVERY_STATUS[d.status]?.label ?? d.status}</Badge>
                    <span>{TRANSITION_LABEL[d.transition] ?? d.transition}</span>
                    <span className="text-slate-500">→ {d.channel_name ?? `canal ${d.channel_id}`}</span>
                    <span className="text-slate-400">
                      {d.attempts} intento(s){d.last_error ? ` · ${d.last_error}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="grid gap-4 border-t border-slate-200 pt-4 sm:grid-cols-2">
            {!can("incident.acknowledge", i.group_id) && (
              <p className="text-xs text-slate-500">Sin permiso para reconocer incidencias de este grupo (solo consulta).</p>
            )}
            {can("incident.acknowledge", i.group_id) && (
            <div>
              <Field label={i.acknowledged ? "Actualizar reconocimiento" : "Reconocer"} htmlFor="ack-comment" hint="No cierra la incidencia ni cambia el estado técnico.">
                <Textarea id="ack-comment" rows={2} maxLength={500} value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comentario (opcional)" />
              </Field>
              <Button
                className="mt-2"
                size="sm"
                icon={<CheckCheck className="h-3.5 w-3.5" />}
                loading={busy === "ack"}
                onClick={() => void act("ack", `admin/incidents/${i.id}/ack`, { comment: comment.trim() || null }, "Incidencia reconocida (sigue abierta hasta que haya recuperación).")}
              >
                Reconocer
              </Button>
            </div>
            )}
            {i.status === "open" && i.manual_resolvable && can("incident.close_queue", i.group_id) && (
              <div>
                <Field label="Cerrar manualmente" htmlFor="res-reason" required hint="Solo para categorías sin evidencia automática (cola local). El motivo queda en el historial.">
                  <Textarea id="res-reason" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Motivo (obligatorio)" />
                </Field>
                <Button
                  className="mt-2"
                  size="sm"
                  variant="secondary"
                  icon={<ShieldCheck className="h-3.5 w-3.5" />}
                  loading={busy === "resolve"}
                  disabled={reason.trim().length < 3}
                  onClick={() => void act("resolve", `admin/incidents/${i.id}/resolve`, { reason: reason.trim() }, "Incidencia cerrada con motivo.")}
                >
                  Cerrar con motivo
                </Button>
              </div>
            )}
            {i.status === "open" && !i.manual_resolvable && (
              <p className="text-xs text-slate-500 sm:pt-6">
                Esta incidencia se resuelve sola cuando hay evidencia de recuperación; no se puede cerrar a mano.
              </p>
            )}
          </div>
        </div>
      ) : null}
    </Modal>
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

function TaskHealthBrief({ h }: { h: TaskHealth }) {
  const st = TASK_STATE[h.state];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
      <Badge tone={st.tone}>{st.label}</Badge>
      <span>Última carga exitosa: {h.last_success_at ? `${fmtDateTz(h.last_success_at)} (${fmtNumber(h.last_success_rows ?? 0)} filas)` : "nunca"}</span>
      <span>Error actual: {h.current_error_code ?? "—"}</span>
      <span>Errores consecutivos: {h.consecutive_failures}</span>
      <span>Watermark: {h.watermark ? `${h.watermark.replace("T", " ")} (sin zona)` : "—"}</span>
    </div>
  );
}

function InstallationHealthBrief({ h }: { h: InstallationHealth }) {
  const c = CONNECTIVITY[h.connectivity];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
      <Badge tone={c.tone}>{c.label}</Badge>
      <span>Último contacto: {h.last_seen_at ? `${fmtDateTz(h.last_seen_at)} (${fmtAgo(h.last_seen_at)})` : "nunca"}</span>
      <span>Cola: {h.queue_depth ?? "—"}</span>
      <span>Apartados: {h.dead_letter_total ?? 0}</span>
    </div>
  );
}
