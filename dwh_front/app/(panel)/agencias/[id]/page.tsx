"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import { ChevronRight, Copy, Eye, Pencil, Plus, RefreshCw } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { Agency, Execution, Incident, ListResponse, Task, TaskHealth } from "@/lib/types";
import { fmtDateTz, fmtDuration, fmtNumber, tzLabel } from "@/lib/format";
import { Badge, Button, Card, IconButton, PageHeader, StatusBadge, Switch } from "@/components/ui/primitives";
import { DataState, NotFoundState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { EXEC_STATUS, IncidentStatusBadges, SeverityBadge, When, WM_KIND_SHORT, useAutoRefresh } from "@/components/health";
import { Breadcrumbs, ExtractorState, SummaryCard, everyLabel, summarize } from "@/components/extractors";
import { TaskFormModal } from "@/components/task-form";
import { CloneTasksModal, type CloneSource } from "@/components/clone-tasks";
import { useActions } from "@/components/use-actions";
import { useSession } from "@/components/session";

const RECENT_EXECUTIONS = 25;

export default function AgenciaDetallePage() {
  const params = useParams<{ id: string }>();
  const id = /^\d+$/.test(params.id ?? "") ? params.id : null;
  const agency = useApi<Agency>(id ? `admin/agencies/${id}` : null);
  const health = useApi<{ items: TaskHealth[] }>(id ? `admin/health/tasks?agency_id=${id}` : null);
  // Configuración completa (SQL, modo empresa…) para el formulario de edición: una sola lista por agencia.
  const tasks = useApi<ListResponse<Task>>(id ? `admin/tasks?agency_id=${id}` : null);
  const execs = useApi<{ total: number; items: Execution[] }>(id ? `admin/executions${qs({ agency_id: id, limit: RECENT_EXECUTIONS })}` : null);
  const incs = useApi<{ total: number; items: Incident[] }>(id ? `admin/incidents${qs({ agency_id: id, view: "open", limit: 50 })}` : null);
  const { can } = useSession();

  const reloadHealth = health.reload;
  const reloadTasks = tasks.reload;
  const reloadExecs = execs.reload;
  const reloadIncs = incs.reload;
  const reloadAgency = agency.reload;
  const reloadConfig = useCallback(() => {
    void reloadHealth();
    void reloadTasks();
  }, [reloadHealth, reloadTasks]);
  const reloadLive = useCallback(() => {
    void reloadHealth();
    void reloadExecs();
    void reloadIncs();
  }, [reloadHealth, reloadExecs, reloadIncs]);
  const reloadAll = useCallback(() => {
    void reloadAgency();
    void reloadTasks();
    reloadLive();
  }, [reloadAgency, reloadTasks, reloadLive]);
  useAutoRefresh(reloadLive);
  const { run, busy } = useActions(reloadConfig);

  const [editing, setEditing] = useState<Task | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [cloneSource, setCloneSource] = useState<CloneSource | null>(null);

  const items = useMemo(() => health.data?.items ?? [], [health.data]);
  const summary = useMemo(() => summarize(items), [items]);
  const taskById = useMemo(() => new Map((tasks.data?.items ?? []).map((t) => [t.id, t])), [tasks.data]);

  if (!id || agency.status === 404) {
    return <NotFoundState title="Agencia no encontrada" backHref="/agencias" backLabel="Volver a Agencias" />;
  }

  const a = agency.data;
  const canCfg = a ? can("config.manage", a.group_id) : false;
  const execItems = execs.data?.items ?? [];
  const incItems = incs.data?.items ?? [];
  const scopeQs = a ? qs({ group_id: a.group_id, company_id: a.company_id, agency_id: a.id }) : "";

  return (
    <>
      <Breadcrumbs
        items={[
          { label: "Grupos", href: "/grupos" },
          { label: a?.group_name ?? "…", href: a ? `/grupos/${a.group_id}` : undefined },
          { label: a?.company_name ?? "…", href: a ? `/agencias${qs({ group_id: a.group_id, company_id: a.company_id })}` : undefined },
          { label: a?.name ?? "…" },
        ]}
      />
      <PageHeader
        title={a?.name ?? "Agencia"}
        description={`Extractores de la agencia, su salud, ejecuciones recientes e incidencias abiertas. Horas en ${tzLabel()}; se actualiza cada 30 s.`}
        actions={
          <>
            <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reloadAll} loading={health.loading && Boolean(health.data)}>
              Actualizar
            </Button>
            {canCfg && a && items.length > 0 && (
              <Button
                variant="secondary"
                icon={<Copy className="h-4 w-4" />}
                onClick={() =>
                  setCloneSource({
                    kind: "agency",
                    agencyId: a.id,
                    agencyName: a.name,
                    tasks: items.map((t) => ({ id: t.task_id, object_name: t.object_name, destination_table: t.destination_table })),
                  })
                }
              >
                Clonar a otras agencias
              </Button>
            )}
            {canCfg && (
              <Button
                icon={<Plus className="h-4 w-4" />}
                onClick={() => {
                  setEditing(null);
                  setFormOpen(true);
                }}
              >
                Nuevo extractor
              </Button>
            )}
          </>
        }
      />

      <DataState loading={agency.loading} error={agency.error} hasData={Boolean(a)} empty={false} onRetry={agency.reload}>
        {a && (
          <>
            <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
              <StatusBadge enabled={a.is_enabled} on="Agencia habilitada" off="Agencia deshabilitada" />
              <span className="text-xs text-slate-500">
                Empresa <b className="font-medium text-slate-700">{a.company_name}</b> · Grupo{" "}
                <Link href={`/grupos/${a.group_id}`} className="font-medium text-brand-700 hover:underline">
                  {a.group_name}
                </Link>
              </span>
              <span className="text-xs text-slate-500">{a.has_token ? "Con token de agencia" : "Sin token de agencia"}</span>
            </div>
            <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
              <SummaryCard label="Extractores activos" value={fmtNumber(summary.active)} hint={`de ${fmtNumber(summary.total)}`} tone="green" />
              <SummaryCard label="Con error" value={fmtNumber(summary.failing)} tone={summary.failing ? "red" : "slate"} />
              <SummaryCard label="Retrasados" value={fmtNumber(summary.delayed)} tone={summary.delayed ? "amber" : "slate"} />
              <SummaryCard label="Sin ejecutar" value={fmtNumber(summary.neverRun)} />
            </div>
          </>
        )}
      </DataState>

      {a && (
        <>
          {/* ── Extractores ─────────────────────────────────────────────── */}
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h2 className="text-base font-semibold text-slate-900">Extractores</h2>
            {health.data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(items.length)} extractor(es)</span>}
          </div>
          <Card>
            <DataState
              loading={health.loading}
              error={health.error}
              hasData={Boolean(health.data)}
              empty={items.length === 0}
              onRetry={health.reload}
              emptyTitle="Esta agencia no tiene extractores"
              emptyDescription={canCfg ? "Crea uno con «Nuevo extractor»." : "Aún no se le ha asignado ningún objeto del catálogo."}
            >
              <Table>
                <thead>
                  <tr>
                    <Th>Extractor → tabla destino</Th>
                    <Th>Estado</Th>
                    <Th>Última carga exitosa</Th>
                    <Th>Última ejecución</Th>
                    <Th>Punto de sincronización</Th>
                    <Th>Error actual</Th>
                    <Th>Programación</Th>
                    <Th>Activo</Th>
                    <Th className="text-right">Acciones</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((t) => {
                    const le = t.last_execution;
                    const full = taskById.get(t.task_id) ?? null;
                    return (
                      <Tr key={t.task_id} className={t.state === "disabled" ? "opacity-70" : undefined}>
                        <Td>
                          <p className="font-medium text-slate-900">{t.object_name}</p>
                          <p className="font-mono text-[11px] text-slate-500">
                            → {t.destination_table} · #{t.task_id}
                          </p>
                          {full && !full.run_on_company_token && <Badge tone="amber" className="mt-1">Solo agencia/grupo</Badge>}
                        </Td>
                        <Td>
                          <ExtractorState t={t} />
                        </Td>
                        <Td>
                          <When value={t.last_success_at} />
                          {t.last_success_at && <p className="text-[11px] text-slate-500">{fmtNumber(t.last_success_rows ?? 0)} filas</p>}
                        </Td>
                        <Td>
                          {le ? (
                            <div>
                              <Badge tone={EXEC_STATUS[le.status]?.tone ?? "slate"}>{EXEC_STATUS[le.status]?.label ?? le.status}</Badge>
                              <div className="mt-1">
                                <When value={le.finished_at ?? le.started_at} />
                              </div>
                            </div>
                          ) : (
                            <span className="text-xs text-slate-400">nunca</span>
                          )}
                        </Td>
                        <Td>
                          {t.watermark ? (
                            <div>
                              <p className="whitespace-nowrap font-mono text-xs text-slate-700">{t.watermark.replace("T", " ")}</p>
                              <p className="text-[11px] text-slate-500">sin zona · {WM_KIND_SHORT[t.watermark_kind ?? ""] ?? t.watermark_kind ?? "—"}</p>
                            </div>
                          ) : (
                            <span className="text-xs text-slate-400">sin watermark</span>
                          )}
                        </Td>
                        <Td>
                          {t.current_error_code ? <p className="font-mono text-xs text-red-700">{t.current_error_code}</p> : <span className="text-xs text-slate-400">—</span>}
                          {t.consecutive_failures > 0 && (
                            <p className="text-[11px] font-semibold text-red-600">{t.consecutive_failures} error(es) consecutivo(s)</p>
                          )}
                          {t.failing_installations.length > 0 && (
                            <ul className="mt-1 space-y-0.5 text-[11px] text-slate-500" title="Fallas abiertas por instalación">
                              {t.failing_installations.map((f) => (
                                <li key={f.incident_id}>
                                  <Link href={`/incidencias?id=${f.incident_id}`} className="hover:underline">
                                    {f.installation_name ?? "—"}: <span className="font-mono text-red-700">{f.error_code ?? "?"}</span> ×{f.occurrences}
                                  </Link>
                                </li>
                              ))}
                            </ul>
                          )}
                        </Td>
                        <Td className="whitespace-nowrap text-xs">{everyLabel(t.schedule_seconds)}</Td>
                        <Td>
                          {canCfg ? (
                            <Switch
                              checked={t.is_active}
                              disabled={busy === "toggle"}
                              hideLabel
                              label={t.is_active ? `Desactivar extractor ${t.object_name}` : `Activar extractor ${t.object_name}`}
                              onChange={(v) =>
                                run("toggle", `admin/tasks/${t.task_id}/${v ? "enable" : "disable"}`, {
                                  success: v ? "Extractor activado." : "Extractor desactivado.",
                                })
                              }
                            />
                          ) : (
                            <StatusBadge enabled={t.is_active} />
                          )}
                        </Td>
                        <Td className="text-right">
                          <div className="flex justify-end gap-0.5">
                          {canCfg && (
                            <IconButton
                              label="Clonar a otras agencias"
                              onClick={() => setCloneSource({ kind: "task", taskId: t.task_id, objectName: t.object_name, agencyId: t.agency_id, agencyName: t.agency_name })}
                            >
                              <Copy className="h-4 w-4" />
                            </IconButton>
                          )}
                          <IconButton
                            label={canCfg ? "Editar extractor" : "Ver extractor (solo lectura)"}
                            disabled={!full}
                            onClick={() => {
                              setEditing(full);
                              setFormOpen(true);
                            }}
                          >
                            {canCfg ? <Pencil className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                          </IconButton>
                          </div>
                        </Td>
                      </Tr>
                    );
                  })}
                </tbody>
              </Table>
            </DataState>
          </Card>

          {/* ── Incidencias abiertas ───────────────────────────────────── */}
          <div className="mb-3 mt-8 flex flex-wrap items-center gap-3">
            <h2 className="text-base font-semibold text-slate-900">Incidencias abiertas</h2>
            <Link href={`/incidencias${scopeQs}`} className="inline-flex items-center text-xs font-medium text-brand-600 hover:text-brand-700 sm:ml-auto">
              Ver en Incidencias <ChevronRight className="h-3.5 w-3.5" />
            </Link>
          </div>
          <Card>
            <DataState
              loading={incs.loading}
              error={incs.error}
              hasData={Boolean(incs.data)}
              empty={incItems.length === 0}
              onRetry={incs.reload}
              emptyTitle="Sin incidencias abiertas"
              emptyDescription="Ningún extractor ni instalación de esta agencia tiene incidencias abiertas."
            >
              <Table>
                <thead>
                  <tr>
                    <Th>Incidencia</Th>
                    <Th>Severidad</Th>
                    <Th>Estado</Th>
                    <Th>Última ocurrencia</Th>
                    <Th className="text-right">Ocurrencias</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {incItems.map((i) => (
                    <Tr key={i.id}>
                      <Td>
                        <Link href={`/incidencias?id=${i.id}`} className="font-medium text-slate-900 hover:text-brand-700 hover:underline">
                          #{i.id} {i.category_label}
                        </Link>
                        <p className="text-xs text-slate-500">
                          {[i.object_name && `${i.object_name}${i.task_id ? ` #${i.task_id}` : ""}`, i.installation_name].filter(Boolean).join(" · ") || i.title}
                        </p>
                        {i.last_error_code && <p className="font-mono text-[11px] text-red-700">{i.last_error_code}</p>}
                      </Td>
                      <Td>
                        <SeverityBadge severity={i.severity} />
                      </Td>
                      <Td>
                        <IncidentStatusBadges incident={i} />
                      </Td>
                      <Td>
                        <When value={i.last_seen_at} />
                      </Td>
                      <Td className="text-right tabular-nums">{fmtNumber(i.occurrences)}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            </DataState>
          </Card>

          {/* ── Ejecuciones recientes ──────────────────────────────────── */}
          <div className="mb-3 mt-8 flex flex-wrap items-center gap-3">
            <h2 className="text-base font-semibold text-slate-900">Ejecuciones recientes</h2>
            <span className="text-xs text-slate-500">últimas {RECENT_EXECUTIONS}</span>
            <Link href={`/ejecuciones${scopeQs}`} className="inline-flex items-center text-xs font-medium text-brand-600 hover:text-brand-700 sm:ml-auto">
              Ver todas <ChevronRight className="h-3.5 w-3.5" />
            </Link>
          </div>
          <Card>
            <DataState
              loading={execs.loading}
              error={execs.error}
              hasData={Boolean(execs.data)}
              empty={execItems.length === 0}
              onRetry={execs.reload}
              emptyTitle="Sin ejecuciones"
              emptyDescription="Ningún agente ha reportado ejecuciones de esta agencia (los clientes legados solo aparecen en Eventos)."
            >
              <Table>
                <thead>
                  <tr>
                    <Th>Inicio</Th>
                    <Th>Extractor</Th>
                    <Th>Instalación</Th>
                    <Th>Estado</Th>
                    <Th className="text-right">Filas</Th>
                    <Th className="text-right">Duración</Th>
                    <Th>Error</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {execItems.map((e) => {
                    const st = EXEC_STATUS[e.status] ?? { label: e.status, tone: "slate" as const };
                    return (
                      <Tr key={e.execution_id} className={e.status === "failed" ? "bg-red-50/40" : undefined}>
                        <Td className="whitespace-nowrap text-xs">{fmtDateTz(e.started_at)}</Td>
                        <Td>
                          <p className="text-slate-900">
                            {e.object_name ?? "—"} <span className="text-xs text-slate-400">#{e.task_id}</span>
                          </p>
                        </Td>
                        <Td className="text-xs">{e.installation_name ?? "—"}</Td>
                        <Td>
                          <Badge tone={st.tone}>{st.label}</Badge>
                        </Td>
                        <Td className="whitespace-nowrap text-right text-xs tabular-nums">
                          {fmtNumber(e.rows_loaded)} <span className="text-slate-400">/ {fmtNumber(e.rows_read)} leídas</span>
                        </Td>
                        <Td className="whitespace-nowrap text-right text-xs tabular-nums">{fmtDuration(e.duration_ms)}</Td>
                        <Td>
                          {e.error_code ? <span className="font-mono text-xs text-red-700">{e.error_code}</span> : <span className="text-xs text-slate-400">—</span>}
                        </Td>
                      </Tr>
                    );
                  })}
                </tbody>
              </Table>
            </DataState>
          </Card>

          <CloneTasksModal open={Boolean(cloneSource)} onClose={() => setCloneSource(null)} source={cloneSource} onDone={reloadConfig} />
          <TaskFormModal
            open={formOpen}
            onClose={() => setFormOpen(false)}
            task={editing}
            defaultAgencyId={String(a.id)}
            onSaved={reloadConfig}
            noun="extractor"
          />
        </>
      )}
    </>
  );
}
