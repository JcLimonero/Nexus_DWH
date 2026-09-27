"use client";

import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { ActivityItem } from "@/lib/types";
import { fmtDate, fmtNumber } from "@/lib/format";
import { Badge, Button, Card, PageHeader, Select, Switch } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { ExpandableText } from "@/components/expandable";

function statusTone(code: number) {
  if (code >= 500) return "red" as const;
  if (code >= 400) return "amber" as const;
  return "green" as const;
}

export default function ActividadPage() {
  const [onlyErrors, setOnlyErrors] = useState(true);
  const [limit, setLimit] = useState("200");
  const [search, setSearch] = useState("");
  const { data, loading, error, reload } = useApi<{ total: number; items: ActivityItem[] }>(`monitor/activity${qs({ only_errors: onlyErrors, limit })}`);
  const term = search.trim().toLowerCase();
  const items = (data?.items ?? []).filter(
    (a) => !term || [a.endpoint, a.grupo, a.razon_social, a.client_ip, String(a.status_code)].some((v) => (v || "").toLowerCase().includes(term)),
  );

  return (
    <>
      <PageHeader
        title="Actividad"
        description="Log HTTP del backend (activity_log): peticiones de clientes ETL, monitor y panel."
        actions={
          <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reload} loading={loading && Boolean(data)}>
            Actualizar
          </Button>
        }
      />
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
          placeholder="Buscar endpoint, empresa, IP, código…"
          className="h-9 w-full rounded-md border border-slate-300 px-3 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/20 sm:w-72"
        />
        <Switch checked={onlyErrors} onChange={setOnlyErrors} label="Solo errores (≥ 400)" />
        {data && <span className="text-xs text-slate-500 sm:ml-auto">{fmtNumber(items.length)} registros</span>}
      </div>
      <Card>
        <DataState loading={loading} error={error} hasData={Boolean(data)} empty={items.length === 0} onRetry={reload} emptyTitle="Sin actividad" emptyDescription="No hay registros con los filtros actuales.">
          <Table>
            <thead>
              <tr>
                <Th>Fecha</Th>
                <Th>Petición</Th>
                <Th>Estado</Th>
                <Th className="text-right">ms</Th>
                <Th>Cliente</Th>
                <Th>IP</Th>
                <Th>Detalle</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((a) => (
                <Tr key={a.id}>
                  <Td className="whitespace-nowrap text-xs">{fmtDate(a.timestamp)}</Td>
                  <Td>
                    <span className="mr-1.5 font-mono text-xs font-semibold text-slate-500">{a.method}</span>
                    <code className="font-mono text-xs">{a.endpoint}</code>
                  </Td>
                  <Td>
                    <Badge tone={statusTone(a.status_code)}>{a.status_code}</Badge>
                  </Td>
                  <Td className="text-right tabular-nums">{fmtNumber(a.response_ms)}</Td>
                  <Td>
                    <p className="text-sm">{a.razon_social || "—"}</p>
                    <p className="text-xs text-slate-500">{a.grupo}</p>
                    {a.token && a.token !== "..." && <p className="font-mono text-[11px] text-slate-400">{a.token}</p>}
                  </Td>
                  <Td className="font-mono text-xs">{a.client_ip || "—"}</Td>
                  <Td>
                    <ExpandableText text={a.error_detail} />
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
