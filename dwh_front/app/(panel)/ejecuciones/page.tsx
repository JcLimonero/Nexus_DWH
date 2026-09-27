"use client";

import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { Execution, Installation, ListResponse } from "@/lib/types";
import { fmtDateTz, fmtDuration, fmtNumber, tzLabel } from "@/lib/format";
import { Badge, Button, Card, Input, PageHeader, Select } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ExpandableText } from "@/components/expandable";
import { HierarchyFilters, type FilterValue } from "@/components/filters";
import { useRefData } from "@/components/ref-data";

const STATUS: Record<string, { label: string; tone: "green" | "red" | "amber" | "blue" }> = {
  success: { label: "OK", tone: "green" },
  failed: { label: "Fallida", tone: "red" },
  interrupted: { label: "Interrumpida", tone: "amber" },
  running: { label: "En curso", tone: "blue" },
};

const STAGE_LABEL: Record<string, string> = {
  config: "Configuración",
  extract: "Extracción",
  transform: "Transformación",
  load: "Carga",
  report: "Reporte",
};

/** Reloj del que proviene el watermark (se guarda sin zona horaria). */
const WM_KIND: Record<string, string> = {
  source_clock: "hora local del reloj del origen",
  agent_local: "hora local del reloj del agente",
  agent_utc: "hora UTC del reloj del agente",
  legacy_last_run: "last_run_at legado (reloj de Nexus)",
};
const WM_KIND_SHORT: Record<string, string> = {
  source_clock: "origen",
  agent_local: "agente (local)",
  agent_utc: "agente (UTC)",
  legacy_last_run: "legado",
};

/** yyyy-mm-dd (fecha local del navegador) → ISO UTC del inicio de ese día en la zona del navegador. */
function dayStartIso(day: string): string | undefined {
  if (!day) return undefined;
  const d = new Date(`${day}T00:00:00`);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

export default function EjecucionesPage() {
  const [filter, setFilter] = useState<FilterValue>({ group_id: "", company_id: "", agency_id: "" });
  const [status, setStatus] = useState("");
  const [stage, setStage] = useState("");
  const [installation, setInstallation] = useState("");
  const [taskId, setTaskId] = useState("");
  const [since, setSince] = useState("");
  const [limit, setLimit] = useState("200");
  const { groups, companies, agencies } = useRefData({ agencies: true });
  const installs = useApi<ListResponse<Installation>>("admin/installations");
  const { data, loading, error, reload } = useApi<{ total: number; items: Execution[] }>(
    `admin/executions${qs({
      ...filter,
      status,
      failure_stage: stage,
      installation_id: installation,
      task_id: /^\d+$/.test(taskId) ? taskId : "",
      since: dayStartIso(since),
      limit,
    })}`,
  );
  const items = data?.items ?? [];
  const failed = items.filter((e) => e.status === "failed").length;

  return (
    <>
      <PageHeader
        title="Ejecuciones"
        description={`Historial de ejecuciones reportadas por los agentes (una fila por intento). Horas en ${tzLabel()}.`}
        actions={
          <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reload} loading={loading && Boolean(data)}>
            Actualizar
          </Button>
        }
      />
      <div className="mb-4 space-y-3">
        <HierarchyFilters value={filter} onChange={setFilter} groups={groups} companies={companies} agencies={agencies} />
        <div className="flex flex-wrap items-center gap-2">
          <Select aria-label="Estado" className="w-full sm:w-40" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Todos los estados</option>
            {Object.entries(STATUS).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
          </Select>
          <Select aria-label="Etapa" className="w-full sm:w-44" value={stage} onChange={(e) => setStage(e.target.value)}>
            <option value="">Todas las etapas</option>
            {Object.entries(STAGE_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
          <Select aria-label="Instalación" className="w-full sm:w-52" value={installation} onChange={(e) => setInstallation(e.target.value)}>
            <option value="">Todas las instalaciones</option>
            {(installs.data?.items ?? []).map((i) => (
              <option key={i.id} value={i.id}>
                {i.name} ({i.id.slice(0, 8)})
              </option>
            ))}
          </Select>
          <Input aria-label="Tarea #" placeholder="Tarea #" className="w-full sm:w-28" value={taskId} onChange={(e) => setTaskId(e.target.value)} />
          <Input aria-label="Desde" type="date" className="w-full sm:w-44" value={since} onChange={(e) => setSince(e.target.value)} />
          <Select aria-label="Límite" className="w-full sm:w-36" value={limit} onChange={(e) => setLimit(e.target.value)}>
            {["50", "200", "500", "1000", "2000"].map((l) => (
              <option key={l} value={l}>
                Últimas {l}
              </option>
            ))}
          </Select>
          {data && (
            <span className="text-xs text-slate-500 sm:ml-auto">
              {fmtNumber(items.length)} ejecución(es) · {failed} fallida(s) en la vista
            </span>
          )}
        </div>
      </div>
      <Card>
        <DataState
          loading={loading}
          error={error}
          hasData={Boolean(data)}
          empty={items.length === 0}
          onRetry={reload}
          emptyTitle="Sin ejecuciones"
          emptyDescription="No hay ejecuciones con los filtros actuales (los agentes legados solo aparecen en Eventos)."
        >
          <Table>
            <thead>
              <tr>
                <Th>Inicio</Th>
                <Th>Tarea</Th>
                <Th>Instalación</Th>
                <Th>Estado</Th>
                <Th className="text-right">Filas</Th>
                <Th className="text-right">Duración</Th>
                <Th>Error / avisos</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((e) => {
                const st = STATUS[e.status] ?? { label: e.status, tone: "slate" as const };
                return (
                  <Tr key={e.execution_id} className={e.status === "failed" ? "bg-red-50/40" : undefined}>
                    <Td className="whitespace-nowrap text-xs">
                      {fmtDateTz(e.started_at)}
                      <p className="mt-0.5 font-mono text-[11px] text-slate-400" title={e.execution_id}>
                        {e.execution_id.slice(0, 8)}… · intento {e.attempt}
                      </p>
                    </Td>
                    <Td>
                      <p className="font-medium text-slate-900">
                        {e.object_name ?? "—"} <span className="text-xs font-normal text-slate-400">#{e.task_id}</span>
                      </p>
                      <p className="text-xs text-slate-500">{[e.group_name, e.company_name, e.agency_name].filter(Boolean).join(" / ")}</p>
                      <p className="font-mono text-[11px] text-slate-400">
                        {e.destination_table} · query v{e.query_version ?? "?"}
                      </p>
                    </Td>
                    <Td className="text-xs">
                      <p className="text-slate-700">{e.installation_name ?? "—"}</p>
                      <p className="font-mono text-[11px] text-slate-400">v{e.client_version || "?"}</p>
                    </Td>
                    <Td>
                      <Badge tone={st.tone}>{st.label}</Badge>
                      {e.failure_stage && <p className="mt-1 text-xs text-slate-500">{STAGE_LABEL[e.failure_stage] ?? e.failure_stage}</p>}
                    </Td>
                    <Td className="whitespace-nowrap text-right text-xs tabular-nums">
                      <p>
                        {fmtNumber(e.rows_loaded)} <span className="text-slate-400">/ {fmtNumber(e.rows_read)} leídas</span>
                      </p>
                      {(e.rows_inserted !== null || e.rows_updated !== null) && (
                        <p className="text-slate-400">
                          +{fmtNumber(e.rows_inserted)} ins · {fmtNumber(e.rows_updated)} act
                        </p>
                      )}
                      {e.checkpoint_confirmed && (
                        <p className="text-[11px] text-slate-400" title={`Watermark confirmado (${WM_KIND[e.checkpoint_kind ?? ""] ?? e.checkpoint_kind ?? "?"}); sin zona horaria`}>
                          wm {e.checkpoint_confirmed.replace("T", " ").slice(0, 19)} · {WM_KIND_SHORT[e.checkpoint_kind ?? ""] ?? e.checkpoint_kind}
                        </p>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap text-right text-xs tabular-nums">{fmtDuration(e.duration_ms)}</Td>
                    <Td>
                      {e.error_code && <p className="font-mono text-xs font-semibold text-red-700">{e.error_code}</p>}
                      <ExpandableText text={e.error_message_sanitized} />
                      {e.warnings?.length ? (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {e.warnings.map((w) => (
                            <Badge key={w} tone="amber">
                              {w}
                            </Badge>
                          ))}
                        </div>
                      ) : null}
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
