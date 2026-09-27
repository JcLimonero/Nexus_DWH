"use client";

import { useState } from "react";
import { CheckCheck, RefreshCw } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { ClientEvent } from "@/lib/types";
import { fmtDate, fmtNumber } from "@/lib/format";
import { Badge, Button, Card, PageHeader, Select, Switch } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ExpandableText } from "@/components/expandable";
import { useActions } from "@/components/use-actions";

export default function EventosPage() {
  const [eventType, setEventType] = useState("");
  const [onlyPending, setOnlyPending] = useState(false);
  const [limit, setLimit] = useState("200");
  const [search, setSearch] = useState("");
  const { data, loading, error, reload } = useApi<{ total: number; items: ClientEvent[] }>(
    `monitor/events${qs({ event_type: eventType, only_unacknowledged: onlyPending || undefined, limit })}`,
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
            <Button
              icon={<CheckCheck className="h-4 w-4" />}
              loading={busy === "ackall"}
              onClick={() =>
                run<{ acknowledged: number }>("ackall", "monitor/events/ack-all", {
                  method: "PUT",
                  success: "Errores reconocidos.",
                  confirm: { title: "Reconocer todos", message: "Se marcarán como reconocidos todos los errores pendientes.", confirmLabel: "Reconocer todos" },
                })
              }
            >
              Reconocer todos
            </Button>
          </>
        }
      />
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Select aria-label="Tipo de evento" className="w-full sm:w-40" value={eventType} onChange={(e) => setEventType(e.target.value)}>
          <option value="">Todos</option>
          <option value="error">Errores</option>
          <option value="ok">OK</option>
        </Select>
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
        <Switch checked={onlyPending} onChange={setOnlyPending} label="Solo sin reconocer" />
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
                    {e.event_type === "error" && !e.acknowledged ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={busy === `ack-${e.id}`}
                        onClick={() => run(`ack-${e.id}`, `monitor/events/${e.id}/ack`, { method: "PUT", success: "Evento reconocido." })}
                      >
                        Reconocer
                      </Button>
                    ) : (
                      <span className="text-xs text-slate-400">{e.event_type === "error" ? "Reconocido" : "—"}</span>
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
