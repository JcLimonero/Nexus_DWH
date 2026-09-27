"use client";

import { useState } from "react";
import { CheckCheck, RefreshCw } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { ClientEvent } from "@/lib/types";
import { fmtDate, fmtNumber } from "@/lib/format";
import { Badge, Button, Card, PageHeader, Select } from "@/components/ui/primitives";
import { FilterBar, dateRange, useUrlFilters } from "@/components/scope-filters";
import { useSession } from "@/components/session";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ExpandableText } from "@/components/expandable";
import { useActions } from "@/components/use-actions";

const FILTER_KEYS = ["group_id", "company_id", "agency_id", "task_id", "status", "since", "until"] as const;
const STATUS_OPTIONS = [
  { value: "error", label: "Errores" },
  { value: "pending", label: "Errores sin reconocer" },
  { value: "ok", label: "OK" },
];

export default function EventosPage() {
  const { can, canAny } = useSession();
  const [f, setF, clearF] = useUrlFilters(FILTER_KEYS);
  const [limit, setLimit] = useState("200");
  const [search, setSearch] = useState("");
  const { data, loading, error, reload } = useApi<{ total: number; items: ClientEvent[] }>(
    `admin/events${qs({
      group_id: f.group_id,
      company_id: f.company_id,
      agency_id: f.agency_id,
      task_id: f.task_id,
      event_type: f.status === "pending" ? "error" : f.status,
      only_unacknowledged: f.status === "pending" || undefined,
      ...dateRange(f.since, f.until),
      limit,
    })}`,
  );
  const { run, busy } = useActions(reload);
  const term = search.trim().toLowerCase();
  const items = (data?.items ?? []).filter(
    (e) => !term || [e.grupo, e.razon_social, e.task_name, e.detail, e.config_id].some((v) => (v || "").toLowerCase().includes(term)),
  );
  const pending = (data?.items ?? []).filter((e) => e.event_type === "error" && !e.acknowledged).length;

  return (
    <>
      <PageHeader
        title="Eventos"
        description="Resultados (ok / error) reportados por los clientes ETL."
        actions={
          <>
            <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reload} loading={loading && Boolean(data)}>
              Actualizar
            </Button>
            {canAny("incident.acknowledge") && (
              <Button
                icon={<CheckCheck className="h-4 w-4" />}
                loading={busy === "ackall"}
                onClick={() =>
                  run<{ acknowledged: number }>(
                    "ackall",
                    `admin/events/ack-all${qs({ group_id: f.group_id, company_id: f.company_id, agency_id: f.agency_id })}`,
                    {
                      method: "PUT",
                      success: "Errores reconocidos.",
                      confirm: {
                        title: "Reconocer todos",
                        message:
                          "Se marcarán como reconocidos los errores pendientes de los grupos donde usted puede reconocer (y del grupo/empresa/agencia filtrados).",
                        confirmLabel: "Reconocer todos",
                      },
                    },
                  )
                }
              >
                Reconocer todos
              </Button>
            )}
          </>
        }
      />
      <div className="mb-3">
        <FilterBar
          values={f}
          onChange={setF}
          onClear={clearF}
          fields={[...FILTER_KEYS]}
          statusOptions={STATUS_OPTIONS}
          statusLabel="Tipo"
        />
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Select aria-label="Límite" className="w-full sm:w-36" value={limit} onChange={(e) => setLimit(e.target.value)}>
          {["50", "200", "500", "1000", "2000"].map((l) => (
            <option key={l} value={l}>
              Últimos {l}
            </option>
          ))}
        </Select>
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Buscar grupo, empresa, tarea, detalle…"
          className="h-9 w-full rounded-md border border-slate-300 px-3 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/20 sm:w-72"
        />
        {data && (
          <span className="text-xs text-slate-500 sm:ml-auto">
            {fmtNumber(items.length)} de {fmtNumber(data.total)} · {pending} errores pendientes en la vista
          </span>
        )}
      </div>
      <Card>
        <DataState loading={loading} error={error} hasData={Boolean(data)} empty={items.length === 0} onRetry={reload} emptyTitle="Sin eventos" emptyDescription="No hay eventos con los filtros actuales.">
          <Table>
            <thead>
              <tr>
                <Th>Fecha</Th>
                <Th>Tipo</Th>
                <Th>Cliente / tarea</Th>
                <Th className="text-right">Filas</Th>
                <Th>Detalle</Th>
                <Th className="text-right">Estado</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((e) => (
                <Tr key={e.id} className={e.event_type === "error" && !e.acknowledged ? "bg-red-50/40" : undefined}>
                  <Td className="whitespace-nowrap text-xs">{fmtDate(e.timestamp)}</Td>
                  <Td>{e.event_type === "ok" ? <Badge tone="green">OK</Badge> : <Badge tone="red">Error</Badge>}</Td>
                  <Td>
                    <p className="font-medium text-slate-900">{e.razon_social || "—"}</p>
                    <p className="text-xs text-slate-500">{e.grupo}</p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      {e.task_name} {e.config_id && <span className="text-slate-400">(#{e.config_id})</span>}
                    </p>
                  </Td>
                  <Td className="text-right tabular-nums">{fmtNumber(e.rows_loaded)}</Td>
                  <Td>
                    <ExpandableText text={e.detail} />
                  </Td>
                  <Td className="text-right">
                    {e.event_type === "error" && !e.acknowledged && can("incident.acknowledge", e.group_id) ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={busy === `ack-${e.id}`}
                        onClick={() => run(`ack-${e.id}`, `admin/events/${e.id}/ack`, { method: "PUT", success: "Evento reconocido." })}
                      >
                        Reconocer
                      </Button>
                    ) : (
                      <span className="text-xs text-slate-400" title={e.acknowledged_at ? fmtDate(e.acknowledged_at) : undefined}>
                        {e.event_type !== "error"
                          ? "—"
                          : e.acknowledged
                            ? `Reconocido${e.acknowledged_by ? ` por ${e.acknowledged_by}` : ""}`
                            : "Pendiente"}
                      </span>
                    )}
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
