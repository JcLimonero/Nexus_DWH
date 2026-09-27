"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, Copy, Plus, RefreshCw, Search } from "lucide-react";
import { useApi } from "@/lib/api";
import type { Agency, Group, ListResponse, TaskHealth } from "@/lib/types";
import { cx, fmtNumber, tzLabel } from "@/lib/format";
import { Badge, Button, Card, IconButton, Input, PageHeader, StatusBadge, Switch } from "@/components/ui/primitives";
import { DataState, EmptyState, NotFoundState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { FilterBar, useUrlFilters } from "@/components/scope-filters";
import { TASK_STATE, When, useAutoRefresh } from "@/components/health";
import { Breadcrumbs, ExtractorState, SummaryCard, everyLabel, matchesSearch, matchesState, summarize } from "@/components/extractors";
import { useActions } from "@/components/use-actions";
import { useSession } from "@/components/session";
import { TaskFormModal } from "@/components/task-form";
import { CloneTasksModal, type CloneSource } from "@/components/clone-tasks";

const FILTER_KEYS = ["status", "q"] as const;

interface CompanyBlock {
  id: number;
  name: string;
  agencies: Agency[];
}

export default function GrupoDetallePage() {
  const params = useParams<{ id: string }>();
  const id = /^\d+$/.test(params.id ?? "") ? params.id : null;
  const group = useApi<Group>(id ? `admin/groups/${id}` : null);
  const agencies = useApi<ListResponse<Agency>>(id ? `admin/agencies?group_id=${id}` : null);
  const health = useApi<{ items: TaskHealth[] }>(id ? `admin/health/tasks?group_id=${id}` : null);
  const { can } = useSession();
  const [f, setF, clearF] = useUrlFilters(FILTER_KEYS);
  const reloadHealth = health.reload;
  const reloadAgencies = agencies.reload;
  const reloadAll = useCallback(() => {
    void reloadHealth();
    void reloadAgencies();
  }, [reloadHealth, reloadAgencies]);
  useAutoRefresh(reloadHealth);
  const { run, busy } = useActions(reloadHealth);

  // Búsqueda: estado local (escritura fluida) que se guarda en la URL con un pequeño retraso.
  const [search, setSearch] = useState(f.q);
  const pushed = useRef(f.q);
  useEffect(() => {
    // Solo cambios externos (Limpiar, atrás/adelante): no pisa lo que se está escribiendo.
    if (f.q !== pushed.current) {
      pushed.current = f.q;
      setSearch(f.q);
    }
  }, [f.q]);
  useEffect(() => {
    if (search === pushed.current) return;
    const h = setTimeout(() => {
      pushed.current = search;
      setF({ q: search });
    }, 250);
    return () => clearTimeout(h);
  }, [search, setF]);

  const tasks = useMemo(() => health.data?.items ?? [], [health.data]);
  const summary = useMemo(() => summarize(tasks), [tasks]);
  const filtering = Boolean(f.status || f.q.trim());

  const byAgency = useMemo(() => {
    const m = new Map<number, TaskHealth[]>();
    tasks
      .filter((t) => matchesState(t, f.status) && matchesSearch(t, f.q))
      .forEach((t) => m.set(t.agency_id, [...(m.get(t.agency_id) ?? []), t]));
    return m;
  }, [tasks, f.status, f.q]);

  const companies = useMemo<CompanyBlock[]>(() => {
    const needle = f.q.trim().toLowerCase();
    const out = new Map<number, CompanyBlock>();
    (agencies.data?.items ?? []).forEach((a) => {
      const visible =
        !filtering || (byAgency.get(a.id)?.length ?? 0) > 0 || (!f.status && needle && a.name.toLowerCase().includes(needle));
      if (!visible) return;
      const c = out.get(a.company_id) ?? { id: a.company_id, name: a.company_name, agencies: [] };
      c.agencies.push(a);
      out.set(a.company_id, c);
    });
    return Array.from(out.values()).sort((x, y) => x.name.localeCompare(y.name, "es"));
  }, [agencies.data, byAgency, filtering, f.status, f.q]);

  // Alta de extractor (agencia preseleccionada o a elegir dentro del grupo) y clonado.
  const [newFor, setNewFor] = useState<string | null>(null);
  const [cloneSource, setCloneSource] = useState<CloneSource | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const toggle = (key: string) =>
    setCollapsed((s) => {
      const n = new Set(s);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });
  const allKeys = companies.flatMap((c) => [`c${c.id}`, ...c.agencies.map((a) => `a${a.id}`)]);

  if (!id || group.status === 404) {
    return <NotFoundState title="Grupo no encontrado" backHref="/grupos" backLabel="Volver a Grupos" />;
  }

  const g = group.data;
  const canCfg = g ? can("config.manage", g.id) : false;
  const nAgencies = agencies.data?.items.length ?? 0;
  const setState = (s: string) => setF({ status: f.status === s ? "" : s });

  return (
    <>
      <Breadcrumbs items={[{ label: "Grupos", href: "/grupos" }, { label: g?.name ?? "…" }]} />
      <PageHeader
        title={g?.name ?? "Grupo"}
        description={`Extractores de cada agencia del grupo, con su estado de salud. Horas en ${tzLabel()}; se actualiza cada 30 s.`}
        actions={
          <>
            <Button variant="secondary" icon={<RefreshCw className="h-4 w-4" />} onClick={reloadAll} loading={health.loading && Boolean(health.data)}>
              Actualizar
            </Button>
            {canCfg && (
              <Button icon={<Plus className="h-4 w-4" />} onClick={() => setNewFor("")} disabled={nAgencies === 0}>
                Nuevo extractor
              </Button>
            )}
          </>
        }
      />

      <DataState loading={group.loading} error={group.error} hasData={Boolean(g)} empty={false} onRetry={group.reload}>
        {g && (
          <>
            <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
              <StatusBadge enabled={g.is_enabled} on="Grupo habilitado" off="Grupo deshabilitado" />
              {g.secrets_hidden ? (
                <span className="text-xs italic text-slate-400" title="Requiere el permiso «Administrar credenciales» sobre el grupo">
                  Destino DWH oculto (credenciales)
                </span>
              ) : (
                <span className="min-w-0 break-all text-xs text-slate-500">
                  Destino DWH:{" "}
                  <code className="font-mono text-slate-700">
                    {g.warehouse_host || "—"}:{g.warehouse_port}/{g.warehouse_database || "—"}
                  </code>
                </span>
              )}
            </div>

            <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              <SummaryCard label="Agencias" value={fmtNumber(nAgencies)} hint={`${g.company_count} empresa(s)`} />
              <SummaryCard label="Extractores activos" value={fmtNumber(summary.active)} hint={`de ${fmtNumber(summary.total)}`} tone="green" />
              <SummaryCard label="Con error" value={fmtNumber(summary.failing)} tone={summary.failing ? "red" : "slate"} active={f.status === "failing"} onClick={() => setState("failing")} />
              <SummaryCard label="Retrasados" value={fmtNumber(summary.delayed)} tone={summary.delayed ? "amber" : "slate"} active={f.status === "delayed"} onClick={() => setState("delayed")} />
              <SummaryCard label="Sin ejecutar" value={fmtNumber(summary.neverRun)} active={f.status === "never_run"} onClick={() => setState("never_run")} />
            </div>
          </>
        )}
      </DataState>

      {g && (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <FilterBar
              values={f}
              onChange={setF}
              onClear={clearF}
              fields={["status"]}
              statusLabel="Estado"
              statusOptions={Object.entries(TASK_STATE).map(([k, v]) => ({ value: k, label: v.label }))}
            >
              <div className="relative w-full sm:w-72">
                <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-slate-400" aria-hidden />
                <Input
                  type="search"
                  aria-label="Buscar extractor"
                  placeholder="Buscar extractor, tabla o agencia…"
                  className="pl-8"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              {f.q && !f.status && (
                <Button variant="ghost" size="sm" onClick={clearF}>
                  Limpiar
                </Button>
              )}
            </FilterBar>
            {companies.length > 0 && (
              <div className="flex gap-1 sm:ml-auto">
                <Button variant="ghost" size="sm" icon={<ChevronsUpDown className="h-4 w-4" />} onClick={() => setCollapsed(new Set())}>
                  Expandir todo
                </Button>
                <Button variant="ghost" size="sm" icon={<ChevronsDownUp className="h-4 w-4" />} onClick={() => setCollapsed(new Set(allKeys))}>
                  Contraer todo
                </Button>
              </div>
            )}
          </div>

          <DataState
            loading={agencies.loading || health.loading}
            error={agencies.error || health.error}
            hasData={Boolean(agencies.data && health.data)}
            empty={companies.length === 0}
            onRetry={reloadAll}
            emptyTitle={filtering ? "Ningún extractor coincide con los filtros" : "Este grupo no tiene agencias"}
            emptyDescription={filtering ? "Cambia el estado o la búsqueda." : "Da de alta empresas y agencias para asignarles extractores."}
            emptyAction={
              filtering ? (
                <Button size="sm" variant="secondary" onClick={clearF}>
                  Quitar filtros
                </Button>
              ) : undefined
            }
          >
            <div className="space-y-6">
              {companies.map((c) => {
                const cKey = `c${c.id}`;
                const cOpen = !collapsed.has(cKey);
                const cCount = c.agencies.reduce((n, a) => n + (byAgency.get(a.id)?.length ?? 0), 0);
                return (
                  <section key={c.id} aria-label={`Empresa ${c.name}`}>
                    <button
                      type="button"
                      onClick={() => toggle(cKey)}
                      aria-expanded={cOpen}
                      className="mb-2 flex w-full min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 rounded-md text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
                    >
                      {cOpen ? <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" /> : <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />}
                      <h2 className="min-w-0 break-words text-base font-semibold text-slate-900">{c.name}</h2>
                      <span className="text-xs text-slate-500">
                        {c.agencies.length} agencia(s) · {cCount} extractor(es)
                      </span>
                    </button>
                    {cOpen && (
                      <div className="space-y-3">
                        {c.agencies.map((a) => (
                          <AgencyBlock
                            key={a.id}
                            agency={a}
                            tasks={byAgency.get(a.id) ?? []}
                            open={!collapsed.has(`a${a.id}`)}
                            onToggle={() => toggle(`a${a.id}`)}
                            canCfg={canCfg}
                            busy={busy === "toggle"}
                            filtering={filtering}
                            onNew={() => setNewFor(String(a.id))}
                            onClone={(t) =>
                              setCloneSource({ kind: "task", taskId: t.task_id, objectName: t.object_name, agencyId: t.agency_id, agencyName: t.agency_name })
                            }
                            onActive={(t, v) =>
                              run("toggle", `admin/tasks/${t.task_id}/${v ? "enable" : "disable"}`, {
                                success: v ? "Extractor activado." : "Extractor desactivado.",
                              })
                            }
                          />
                        ))}
                      </div>
                    )}
                  </section>
                );
              })}
            </div>
          </DataState>

          <TaskFormModal
            open={newFor !== null}
            onClose={() => setNewFor(null)}
            task={null}
            defaultAgencyId={newFor ?? ""}
            groupId={g.id}
            onSaved={reloadAll}
            noun="extractor"
          />
          <CloneTasksModal open={Boolean(cloneSource)} onClose={() => setCloneSource(null)} source={cloneSource} onDone={reloadAll} />
        </>
      )}
    </>
  );
}

function AgencyBlock({
  agency: a,
  tasks,
  open,
  onToggle,
  canCfg,
  busy,
  filtering,
  onActive,
  onNew,
  onClone,
}: {
  agency: Agency;
  tasks: TaskHealth[];
  open: boolean;
  onToggle: () => void;
  canCfg: boolean;
  busy: boolean;
  filtering: boolean;
  onActive: (t: TaskHealth, v: boolean) => void;
  onNew: () => void;
  onClone: (t: TaskHealth) => void;
}) {
  const failing = tasks.filter((t) => t.state === "failing").length;
  const delayed = tasks.filter((t) => matchesState(t, "delayed")).length;
  return (
    <Card>
      <div className={cx("flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2.5 sm:px-4", open && "border-b border-slate-200")}>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="flex min-w-[14rem] flex-1 items-center gap-2 rounded-md text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
        >
          {open ? <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" /> : <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" />}
          <span className="min-w-0 break-words text-sm font-semibold text-slate-900">{a.name}</span>
          <span className="shrink-0 text-xs text-slate-500">
            {tasks.length}
            {filtering ? ` de ${a.task_count}` : ""} extractor(es)
          </span>
        </button>
        <div className="flex flex-wrap items-center gap-1.5">
          {!a.is_enabled && <Badge>Agencia deshabilitada</Badge>}
          {failing > 0 && <Badge tone="red">{failing} con error</Badge>}
          {delayed > 0 && <Badge tone="amber">{delayed} retrasado(s)</Badge>}
          {canCfg && (
            <Button variant="ghost" size="sm" icon={<Plus className="h-3.5 w-3.5" />} onClick={onNew}>
              Nuevo extractor
            </Button>
          )}
          <Link href={`/agencias/${a.id}`} className="inline-flex items-center gap-0.5 text-xs font-medium text-brand-600 hover:text-brand-700">
            Ver agencia <ChevronRight className="h-3.5 w-3.5" />
          </Link>
        </div>
      </div>
      {open &&
        (tasks.length === 0 ? (
          <p className="px-4 py-4 text-sm text-slate-500">
            {filtering ? "Ningún extractor de esta agencia coincide con los filtros." : "Esta agencia no tiene extractores configurados."}
          </p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Extractor → tabla destino</Th>
                <Th>Programación</Th>
                <Th>Estado</Th>
                <Th>Última carga exitosa</Th>
                <Th>Activo</Th>
                {canCfg && <Th className="text-right">Acciones</Th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {tasks.map((t) => (
                <Tr key={t.task_id} className={t.state === "disabled" ? "opacity-70" : undefined}>
                  <Td>
                    <p className="font-medium text-slate-900">{t.object_name}</p>
                    <p className="font-mono text-[11px] text-slate-500">
                      → {t.destination_table} · #{t.task_id}
                    </p>
                  </Td>
                  <Td className="whitespace-nowrap text-xs">{everyLabel(t.schedule_seconds)}</Td>
                  <Td>
                    <ExtractorState t={t} compact />
                  </Td>
                  <Td>
                    <When value={t.last_success_at} />
                    {t.last_success_at && <p className="text-[11px] text-slate-500">{fmtNumber(t.last_success_rows ?? 0)} filas</p>}
                  </Td>
                  <Td>
                    {canCfg ? (
                      <Switch
                        checked={t.is_active}
                        disabled={busy}
                        hideLabel
                        label={t.is_active ? `Desactivar extractor ${t.object_name}` : `Activar extractor ${t.object_name}`}
                        onChange={(v) => onActive(t, v)}
                      />
                    ) : (
                      <StatusBadge enabled={t.is_active} />
                    )}
                  </Td>
                  {canCfg && (
                    <Td className="text-right">
                      <IconButton label={`Clonar ${t.object_name} a otras agencias`} onClick={() => onClone(t)}>
                        <Copy className="h-4 w-4" />
                      </IconButton>
                    </Td>
                  )}
                </Tr>
              ))}
            </tbody>
          </Table>
        ))}
    </Card>
  );
}
