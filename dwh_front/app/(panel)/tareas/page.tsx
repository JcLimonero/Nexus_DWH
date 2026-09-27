"use client";

import Link from "next/link";
import { useState } from "react";
import { Copy, Eye, History, Pencil, Plus, Trash2 } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { ListResponse, Task } from "@/lib/types";
import { fmtDate, fmtSeconds } from "@/lib/format";
import { Badge, Button, Card, IconButton, PageHeader, Switch } from "@/components/ui/primitives";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { useRefData } from "@/components/ref-data";
import { useActions } from "@/components/use-actions";
import { useSession } from "@/components/session";
import { TaskFormModal } from "@/components/task-form";
import { CloneTasksModal, type CloneSource } from "@/components/clone-tasks";
import { FilterBar, useUrlFilters } from "@/components/scope-filters";

const FILTER_KEYS = ["group_id", "company_id", "agency_id"] as const;

export default function TareasPage() {
  const [filter, setFilter, clearFilter] = useUrlFilters(FILTER_KEYS);
  const { data, loading, error, reload } = useApi<ListResponse<Task>>(`admin/tasks${qs(filter)}`);
  const { agencies: allAgencies } = useRefData({ agencies: true });
  const { can, canAny } = useSession();
  const agencies = allAgencies.filter((a) => can("config.manage", a.group_id));
  const { run } = useActions(reload);
  const [editing, setEditing] = useState<Task | null>(null);
  const [open, setOpen] = useState(false);
  const [cloneSource, setCloneSource] = useState<CloneSource | null>(null);
  const items = data?.items ?? [];

  function openCreate() {
    setEditing(null);
    setOpen(true);
  }
  function openEdit(t: Task) {
    setEditing(t);
    setOpen(true);
  }

  return (
    <>
      <PageHeader
        title="Tareas"
        description="Objetos del catálogo asignados a cada agencia, con su SQL de extracción y programación."
        actions={
          canAny("config.manage") ? (
            <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate} disabled={agencies.length === 0}>
              Nueva tarea
            </Button>
          ) : undefined
        }
      />
      <div className="mb-4">
        <FilterBar values={filter} onChange={setFilter} onClear={clearFilter} fields={[...FILTER_KEYS]} />
      </div>
      <Card>
        <DataState loading={loading} error={error} hasData={Boolean(data)} empty={items.length === 0} onRetry={reload} emptyTitle="No hay tareas" emptyDescription="Ajusta los filtros o crea una tarea.">
          <Table>
            <thead>
              <tr>
                <Th>ID</Th>
                <Th>Agencia</Th>
                <Th>Objeto → tabla</Th>
                <Th>Cada</Th>
                <Th>Última ejecución</Th>
                <Th>Estado</Th>
                <Th>Activa</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((t) => {
                const canCfg = can("config.manage", t.group_id);
                return (
                <Tr key={t.id}>
                  <Td className="tabular-nums text-slate-500">{t.id}</Td>
                  <Td>
                    <Link href={`/agencias/${t.agency_id}`} className="font-medium text-slate-900 hover:text-brand-700 hover:underline">
                      {t.agency_name}
                    </Link>
                    <p className="text-xs text-slate-500">
                      {t.company_name} · {t.group_name}
                    </p>
                  </Td>
                  <Td>
                    <p>{t.object_name}</p>
                    <code className="font-mono text-xs text-slate-500">{t.destination_table}</code>
                  </Td>
                  <Td className="whitespace-nowrap">{fmtSeconds(t.schedule_seconds)}</Td>
                  <Td className="whitespace-nowrap text-xs">{t.last_run_at ? fmtDate(t.last_run_at) : <span className="italic text-slate-400">Nunca (carga completa)</span>}</Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {t.effective_active ? <Badge tone="green">Se ejecuta</Badge> : <Badge>No se ejecuta</Badge>}
                      {!t.run_on_company_token && <Badge tone="amber">Solo agencia/grupo</Badge>}
                    </div>
                  </Td>
                  <Td>
                    <Switch
                      checked={t.is_active}
                      disabled={!canCfg}
                      hideLabel
                      label={t.is_active ? "Desactivar tarea" : "Activar tarea"}
                      onChange={(v) => run("toggle", `admin/tasks/${t.id}/${v ? "enable" : "disable"}`, { success: v ? "Tarea activada." : "Tarea desactivada." })}
                    />
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-0.5">
                      <IconButton label={canCfg ? "Editar" : "Ver (solo lectura)"} onClick={() => openEdit(t)}>
                        {canCfg ? <Pencil className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </IconButton>
                      {canCfg && (
                      <>
                      <IconButton
                        label="Clonar a otras agencias"
                        onClick={() => setCloneSource({ kind: "task", taskId: t.id, objectName: t.object_name, agencyId: t.agency_id, agencyName: t.agency_name })}
                      >
                        <Copy className="h-4 w-4" />
                      </IconButton>
                      <IconButton
                        label="Reiniciar última ejecución"
                        disabled={!t.last_run_at}
                        onClick={() =>
                          run("reset", `admin/tasks/${t.id}/reset-last-run`, {
                            success: "Última ejecución reiniciada.",
                            confirm: {
                              title: "Reiniciar última ejecución",
                              message: "La próxima ejecución usará {last_run} = 1900-01-01, es decir, hará una carga completa.",
                              confirmLabel: "Reiniciar",
                            },
                          })
                        }
                      >
                        <History className="h-4 w-4" />
                      </IconButton>
                      <IconButton
                        label="Eliminar"
                        tone="danger"
                        onClick={() =>
                          run("delete", `admin/tasks/${t.id}`, {
                            method: "DELETE",
                            success: "Tarea eliminada.",
                            confirm: { title: "Eliminar tarea", message: `¿Eliminar la tarea ${t.id} (${t.object_name} en ${t.agency_name})?`, confirmLabel: "Eliminar", danger: true },
                          })
                        }
                      >
                        <Trash2 className="h-4 w-4" />
                      </IconButton>
                      </>
                      )}
                    </div>
                  </Td>
                </Tr>
                );
              })}
            </tbody>
          </Table>
        </DataState>
      </Card>

      <CloneTasksModal open={Boolean(cloneSource)} onClose={() => setCloneSource(null)} source={cloneSource} onDone={reload} />
      <TaskFormModal open={open} onClose={() => setOpen(false)} task={editing} defaultAgencyId={filter.agency_id || ""} onSaved={reload} />
    </>
  );
}
