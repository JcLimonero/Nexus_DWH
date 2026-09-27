"use client";

import Link from "next/link";
import { AlertTriangle, ArrowRight, Building2, CheckCircle2, ListChecks, RefreshCw, Store, Users, XCircle } from "lucide-react";
import { useApi } from "@/lib/api";
import { fmtDate, fmtNumber, cx } from "@/lib/format";
import type { ClientEvent, MonitorClient, Stats } from "@/lib/types";
import { Badge, Button, Card, PageHeader } from "@/components/ui/primitives";
import { DataState, ErrorState, LoadingState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";

function StatCard({
  label,
  value,
  sub,
  icon: Icon,
  tone = "slate",
  href,
}: {
  label: string;
  value: number | string;
  sub?: string;
  icon: React.ComponentType<{ className?: string }>;
  tone?: "slate" | "red" | "green";
  href?: string;
}) {
  const body = (
    <Card className={cx("p-4 transition-shadow", href && "hover:shadow-md")}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
          <p className={cx("mt-1 text-2xl font-semibold", tone === "red" ? "text-red-600" : tone === "green" ? "text-emerald-600" : "text-slate-900")}>
            {value}
          </p>
          {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
        </div>
        <div className={cx("rounded-lg p-2", tone === "red" ? "bg-red-50 text-red-500" : "bg-brand-50 text-brand-600")}>
          <Icon className="h-5 w-5" />
        </div>
      </div>
    </Card>
  );
  return href ? <Link href={href}>{body}</Link> : body;
}

function clientHealth(c: MonitorClient): { tone: "green" | "red" | "amber" | "slate"; label: string } {
  if (!c.rs_enabled || !c.grupo_enabled) return { tone: "slate", label: "Deshabilitado" };
  if (c.exec_errors_pending > 0) return { tone: "red", label: "Con errores" };
  if (c.http_errors_1h > 0 || c.exec_errors_1h > 0) return { tone: "amber", label: "Advertencia" };
  if (!c.last_seen && !c.last_execution) return { tone: "slate", label: "Sin conexión" };
  return { tone: "green", label: "OK" };
}

export default function DashboardPage() {
  const stats = useApi<Stats>("admin/stats");
  const clients = useApi<{ clients: MonitorClient[] }>("monitor/clients");
  const errors = useApi<{ items: ClientEvent[] }>("monitor/events?event_type=error&only_unacknowledged=true&limit=8");

  const reloadAll = () => {
    void stats.reload();
    void clients.reload();
    void errors.reload();
  };
  const s = stats.data;

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Estado general de la configuración y de los clientes ETL."
        actions={
          <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reloadAll} loading={stats.loading || clients.loading}>
            Actualizar
          </Button>
        }
      />

      {stats.error && !s ? (
        <Card className="mb-6">
          <ErrorState message={stats.error} onRetry={stats.reload} />
        </Card>
      ) : (
        <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          <StatCard label="Grupos" value={s ? fmtNumber(s.groups) : "…"} sub={s ? `${s.groups_enabled} activos` : undefined} icon={Users} href="/grupos" />
          <StatCard label="Empresas" value={s ? fmtNumber(s.companies) : "…"} sub={s ? `${s.companies_enabled} activas` : undefined} icon={Building2} href="/empresas" />
          <StatCard label="Agencias" value={s ? fmtNumber(s.agencies) : "…"} sub={s ? `${s.agencies_enabled} activas` : undefined} icon={Store} href="/agencias" />
          <StatCard label="Tareas" value={s ? fmtNumber(s.tasks) : "…"} sub={s ? `${s.tasks_active} activas · ${s.objects} objetos` : undefined} icon={ListChecks} href="/tareas" />
          <StatCard
            label="Errores pendientes"
            value={s ? fmtNumber(s.pending_errors) : "…"}
            sub={s ? `${s.errors_24h} errores / ${s.events_24h} eventos en 24 h` : undefined}
            icon={AlertTriangle}
            tone={s && s.pending_errors > 0 ? "red" : "green"}
            href="/eventos"
          />
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <h2 className="text-sm font-semibold text-slate-900">Clientes ETL (por empresa)</h2>
            <span className="text-xs text-slate-500">{clients.data ? `${clients.data.clients.length} clientes` : ""}</span>
          </div>
          <DataState
            loading={clients.loading}
            error={clients.error}
            hasData={Boolean(clients.data)}
            empty={Boolean(clients.data && clients.data.clients.length === 0)}
            onRetry={clients.reload}
            emptyTitle="Aún no hay empresas configuradas"
          >
            <Table>
              <thead>
                <tr>
                  <Th>Empresa</Th>
                  <Th>Estado</Th>
                  <Th>Última conexión</Th>
                  <Th>Última ejecución</Th>
                  <Th className="text-right">Ejecuciones</Th>
                  <Th className="text-right">Errores pend.</Th>
                  <Th className="text-right">Errores 1 h</Th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {clients.data?.clients.map((c) => {
                  const h = clientHealth(c);
                  return (
                    <Tr key={`${c.grupo}-${c.razon_social}`}>
                      <Td>
                        <p className="font-medium text-slate-900">{c.razon_social}</p>
                        <p className="text-xs text-slate-500">{c.grupo}</p>
                      </Td>
                      <Td>
                        <Badge tone={h.tone}>{h.label}</Badge>
                      </Td>
                      <Td className="whitespace-nowrap text-xs">{fmtDate(c.last_seen)}</Td>
                      <Td className="whitespace-nowrap text-xs">{fmtDate(c.last_execution)}</Td>
                      <Td className="text-right tabular-nums">{fmtNumber(c.executions_total)}</Td>
                      <Td className={cx("text-right tabular-nums", c.exec_errors_pending > 0 && "font-semibold text-red-600")}>{c.exec_errors_pending}</Td>
                      <Td className="text-right tabular-nums">{c.exec_errors_1h + c.http_errors_1h}</Td>
                    </Tr>
                  );
                })}
              </tbody>
            </Table>
          </DataState>
        </Card>

        <Card>
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <h2 className="text-sm font-semibold text-slate-900">Errores sin reconocer</h2>
            <Link href="/eventos" className="flex items-center gap-1 text-xs font-medium text-brand-600 hover:text-brand-700">
              Ver eventos <ArrowRight className="h-3 w-3" />
            </Link>
          </div>
          {errors.loading && !errors.data ? (
            <LoadingState />
          ) : errors.error && !errors.data ? (
            <ErrorState message={errors.error} onRetry={errors.reload} />
          ) : errors.data && errors.data.items.length === 0 ? (
            <div className="flex flex-col items-center gap-2 px-4 py-12 text-center">
              <CheckCircle2 className="h-8 w-8 text-emerald-400" />
              <p className="text-sm text-slate-600">Sin errores pendientes.</p>
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {errors.data?.items.map((e) => (
                <li key={e.id} className="px-4 py-3">
                  <div className="flex items-start gap-2">
                    <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-slate-800" title={e.task_name}>
                        {e.task_name || `Tarea ${e.config_id}`}
                      </p>
                      <p className="line-clamp-2 break-words font-mono text-xs text-slate-500">{e.detail}</p>
                      <p className="mt-1 text-[11px] text-slate-400">{fmtDate(e.timestamp)}</p>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}
