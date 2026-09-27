"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { Info, RefreshCw, Siren } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { InstallationHealth, TaskHealth } from "@/lib/types";
import { fmtDateTz, fmtNumber, fmtSeconds, tzLabel } from "@/lib/format";
import { Badge, Button, Card, PageHeader, Select } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { FilterBar, useUrlFilters } from "@/components/scope-filters";

const FILTER_KEYS = ["group_id", "company_id", "agency_id", "task_id", "status"] as const;
import { CATEGORY_LABEL, CONNECTIVITY, EXEC_STATUS, TASK_STATE, WM_KIND_SHORT, When, fmtSecs, useAutoRefresh } from "@/components/health";

const SOURCE_LABEL: Record<string, string> = { configured: "configurada", history: "p90 historial", default: "por defecto" };

export default function SaludPage() {
  const [filter, setFilter, clearFilter] = useUrlFilters(FILTER_KEYS);
  const state = filter.status;
  const [conn, setConn] = useState("");
  const scope = { group_id: filter.group_id, company_id: filter.company_id, agency_id: filter.agency_id };
  const insts = useApi<{ items: InstallationHealth[]; disconnect_after_seconds: number }>(
    `admin/health/installations${qs({ group_id: filter.group_id, company_id: filter.company_id, agency_id: filter.agency_id, connectivity: conn })}`,
  );
  const tasks = useApi<{ items: TaskHealth[] }>(`admin/health/tasks${qs({ ...scope, task_id: filter.task_id, state })}`);
  const reloadInsts = insts.reload;
  const reloadTasks = tasks.reload;
  const reloadAll = useCallback(() => {
    void reloadInsts();
    void reloadTasks();
  }, [reloadInsts, reloadTasks]);
  useAutoRefresh(reloadAll);
  const iItems = insts.data?.items ?? [];
  const tItems = tasks.data?.items ?? [];
  const threshold = insts.data?.disconnect_after_seconds;

  return (
    <>
      <PageHeader
        title="Salud"
        description={`Conectividad de las instalaciones y frescura de cada tarea. Horas en ${tzLabel()}; se actualiza cada 30 s.`}
        actions={
          <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reloadAll} loading={(insts.loading || tasks.loading) && Boolean(insts.data)}>
            Actualizar
          </Button>
        }
      />

      <div className="mb-4">
        <FilterBar
          values={filter}
          onChange={setFilter}
          onClear={clearFilter}
          fields={[...FILTER_KEYS]}
          statusLabel="Estado de tarea"
          statusOptions={Object.entries(TASK_STATE).map(([k, v]) => ({ value: k, label: v.label }))}
        />
      </div>

      <p className="mb-3 flex items-start gap-2 text-sm text-slate-500">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        <span>
          <b>Conectividad</b> (último contacto con Nexus) y <b>éxito del ETL</b> (última carga confirmada) son cosas distintas: una
          instalación puede estar en línea con tareas fallando, y una tarea puede estar al día aunque su última carga haya traído 0 filas.
          {threshold ? ` Se considera sin contacto tras ${fmtSecs(threshold)} sin latidos ni llamadas (lo detecta Nexus).` : ""}
        </span>
      </p>

      {/* ── Instalaciones ─────────────────────────────────────────────── */}
      <div className="mb-3 mt-6 flex flex-wrap items-center gap-3">
        <h2 className="text-base font-semibold text-slate-900">Instalaciones</h2>
        <Select aria-label="Conectividad" className="w-full sm:w-48" value={conn} onChange={(e) => setConn(e.target.value)}>
          <option value="">Toda conectividad</option>
          {Object.entries(CONNECTIVITY).map(([k, v]) => (
            <option key={k} value={k}>
              {v.label}
            </option>
          ))}
        </Select>
        {insts.data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(iItems.length)} instalación(es)</span>}
      </div>
      <Card>
        <DataState
          loading={insts.loading}
          error={insts.error}
          hasData={Boolean(insts.data)}
          empty={iItems.length === 0}
          onRetry={insts.reload}
          emptyTitle="Sin instalaciones"
          emptyDescription="No hay agentes enrolados con estos filtros."
        >
          <Table>
            <thead>
              <tr>
                <Th>Instalación</Th>
                <Th>Conectividad</Th>
                <Th>Último contacto</Th>
                <Th>Última ejecución</Th>
                <Th>Última carga exitosa</Th>
                <Th className="text-right">Cola</Th>
                <Th className="text-right">Incidencias</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {iItems.map((i) => {
                const c = CONNECTIVITY[i.connectivity];
                return (
                  <Tr key={i.id} className={i.status === "revoked" ? "opacity-60" : undefined}>
                    <Td>
                      <p className="font-medium text-slate-900">{i.name}</p>
                      <p className="text-xs text-slate-500">{[i.group_name, i.company_name, i.agency_name].filter(Boolean).join(" / ")}</p>
                      <p className="font-mono text-[11px] text-slate-400">
                        {i.client_version || "—"} · {i.id.slice(0, 8)}…
                      </p>
                    </Td>
                    <Td>
                      <Badge tone={c.tone}>{c.label}</Badge>
                      {i.running.length > 0 && <p className="mt-1 text-xs text-brand-700">{i.running.length} tarea(s) en curso</p>}
                    </Td>
                    <Td>
                      <When value={i.last_seen_at} />
                    </Td>
                    <Td>
                      <When value={i.last_execution_at} />
                    </Td>
                    <Td>
                      <When value={i.last_success_at} />
                    </Td>
                    <Td className="text-right text-xs tabular-nums">
                      <p>{i.queue_depth ?? "—"} en cola</p>
                      {i.dead_letter_total ? <p className="text-amber-700">{i.dead_letter_total} apartados</p> : null}
                      {i.queue_overflow_total ? <p className="text-red-600">{i.queue_overflow_total} descartes</p> : null}
                    </Td>
                    <Td className="text-right">
                      {i.open_incidents > 0 ? (
                        <Link href={`/incidencias?installation_id=${i.id}`} className="inline-flex">
                          <Badge tone="red">
                            <Siren className="h-3 w-3" /> {i.open_incidents}
                          </Badge>
                        </Link>
                      ) : (
                        <span className="text-slate-400">0</span>
                      )}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        </DataState>
      </Card>

      {/* ── Tareas ────────────────────────────────────────────────────── */}
      <div className="mb-3 mt-8 flex flex-wrap items-center gap-3">
        <h2 className="text-base font-semibold text-slate-900">Tareas</h2>
        {tasks.data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(tItems.length)} tarea(s)</span>}
      </div>
      <Card>
        <DataState
          loading={tasks.loading}
          error={tasks.error}
          hasData={Boolean(tasks.data)}
          empty={tItems.length === 0}
          onRetry={tasks.reload}
          emptyTitle="Sin tareas"
          emptyDescription="No hay tareas con estos filtros."
        >
          <Table>
            <thead>
              <tr>
                <Th>Tarea</Th>
                <Th>Estado</Th>
                <Th>Última ejecución</Th>
                <Th>Última carga exitosa</Th>
                <Th>Punto de sincronización confirmado</Th>
                <Th>Error actual</Th>
                <Th className="text-right">Errores consecutivos</Th>
                <Th>Plazos</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {tItems.map((t) => {
                const st = TASK_STATE[t.state];
                const le = t.last_execution;
                return (
                  <Tr key={t.task_id} className={t.state === "disabled" ? "opacity-60" : undefined}>
                    <Td>
                      <p className="font-medium text-slate-900">{t.object_name}</p>
                      <p className="text-xs text-slate-500">
                        {t.group_name} / {t.company_name} /{" "}
                        <Link href={`/agencias/${t.agency_id}`} className="text-brand-700 hover:underline">
                          {t.agency_name}
                        </Link>
                      </p>
                      <p className="font-mono text-[11px] text-slate-400">
                        #{t.task_id} · {t.destination_table}
                      </p>
                    </Td>
                    <Td>
                      <div className="flex flex-col items-start gap-1" title={st.hint}>
                        <Badge tone={st.tone}>{st.label}</Badge>
                        {t.delayed && t.state !== "delayed" && <Badge tone="amber">Retrasada</Badge>}
                        {t.running_long && <Badge tone="amber">Prolongada</Badge>}
                        {t.open_incidents.map((i) => (
                          <Link key={i.id} href={`/incidencias?id=${i.id}`} className="text-[11px] text-red-700 hover:underline">
                            #{i.id} {CATEGORY_LABEL[i.category]}
                            {i.acknowledged ? " (reconocida)" : ""}
                          </Link>
                        ))}
                      </div>
                      {t.running_execution && (
                        <p className="mt-1 text-[11px] text-brand-700">
                          {fmtSecs(t.running_execution.elapsed_seconds)} en {t.running_execution.installation_name ?? "—"}
                          {t.running_execution.confirmed_by_heartbeat ? " · confirmada por latido" : " · iniciando"}
                        </p>
                      )}
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
                      <When value={t.last_success_at} />
                      {t.last_success_at && <p className="text-[11px] text-slate-500">{fmtNumber(t.last_success_rows ?? 0)} filas</p>}
                    </Td>
                    <Td>
                      {t.watermark ? (
                        <div>
                          <p className="whitespace-nowrap font-mono text-xs text-slate-700">{t.watermark.replace("T", " ")}</p>
                          <p className="text-[11px] text-slate-500">
                            sin zona · {WM_KIND_SHORT[t.watermark_kind ?? ""] ?? t.watermark_kind ?? "—"}
                          </p>
                        </div>
                      ) : (
                        <span className="text-xs text-slate-400">sin watermark</span>
                      )}
                    </Td>
                    <Td>
                      {t.current_error_code ? <span className="font-mono text-xs text-red-700">{t.current_error_code}</span> : <span className="text-xs text-slate-400">—</span>}
                      {t.failing_installations.length > 0 && (
                        <ul className="mt-1 space-y-0.5 text-[11px] text-slate-500" title="Fallas abiertas por instalación">
                          {t.failing_installations.map((f) => (
                            <li key={f.incident_id}>
                              <Link href={`/incidencias?id=${f.incident_id}`} className="hover:underline">
                                {f.installation_name ?? "—"}: <span className="font-mono text-red-700">{f.error_code ?? "?"}</span> ×{f.occurrences}
                                {f.acknowledged ? " (reconocida)" : ""}
                              </Link>
                            </li>
                          ))}
                        </ul>
                      )}
                    </Td>
                    <Td className={t.consecutive_failures > 0 ? "text-right font-semibold tabular-nums text-red-600" : "text-right tabular-nums text-slate-500"}>
                      {t.consecutive_failures}
                    </Td>
                    <Td className="text-[11px] text-slate-500">
                      <p>Cada {fmtSeconds(t.schedule_seconds)}</p>
                      <p>
                        Duración esperada {fmtSecs(t.expected_duration_seconds)} ({SOURCE_LABEL[t.expected_duration_source]})
                      </p>
                      <p>Tolerancia {fmtSecs(t.delay_tolerance_seconds)}</p>
                      {t.effective_active && t.delay_deadline_at && <p className="whitespace-nowrap">Retrasada si no carga antes de {fmtDateTz(t.delay_deadline_at)}</p>}
                      {t.effective_active && t.installations_covering === 0 && <p className="text-amber-700">Ninguna instalación activa la cubre</p>}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        </DataState>
      </Card>
    </>
  );
}
