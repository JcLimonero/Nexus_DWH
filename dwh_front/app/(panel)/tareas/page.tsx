"use client";

import { useMemo, useState, type FormEvent } from "react";
import { History, Pencil, Plus, Trash2 } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { Agency, CatalogObject, ListResponse, Task } from "@/lib/types";
import { fmtDate, fmtSeconds } from "@/lib/format";
import { Badge, Button, Card, Field, IconButton, Input, PageHeader, Select, Switch, Textarea } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { HierarchyFilters, type FilterValue } from "@/components/filters";
import { useRefData } from "@/components/ref-data";
import { useActions } from "@/components/use-actions";
import { useToast } from "@/components/ui/feedback";

interface FormState {
  agency_id: string;
  object_catalog_id: string;
  extract_sql: string;
  schedule_seconds: string;
  is_active: boolean;
  run_on_company_token: boolean;
}

const EMPTY: FormState = {
  agency_id: "",
  object_catalog_id: "",
  extract_sql: "",
  schedule_seconds: "3600",
  is_active: true,
  run_on_company_token: true,
};

const PRESETS = [
  { label: "5 min", value: 300 },
  { label: "15 min", value: 900 },
  { label: "1 h", value: 3600 },
  { label: "6 h", value: 21600 },
  { label: "24 h", value: 86400 },
];

function AgencyOptions({ agencies }: { agencies: Agency[] }) {
  const groups = new Map<string, Agency[]>();
  agencies.forEach((a) => {
    const k = `${a.group_name} / ${a.company_name}`;
    groups.set(k, [...(groups.get(k) ?? []), a]);
  });
  return (
    <>
      {Array.from(groups.entries()).map(([k, list]) => (
        <optgroup key={k} label={k}>
          {list.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </optgroup>
      ))}
    </>
  );
}

export default function TareasPage() {
  const [filter, setFilter] = useState<FilterValue>({ group_id: "", company_id: "", agency_id: "" });
  const { data, loading, error, reload } = useApi<ListResponse<Task>>(`admin/tasks${qs(filter as unknown as Record<string, string>)}`);
  const { groups, companies, agencies } = useRefData({ agencies: true });
  const objects = useApi<ListResponse<CatalogObject>>("admin/objects");
  const { run } = useActions(reload);
  const toast = useToast();
  const [editing, setEditing] = useState<Task | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const items = data?.items ?? [];

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  const selectedAgency = agencies.find((a) => String(a.id) === form.agency_id);
  const objectOptions = useMemo(
    () => (objects.data?.items ?? []).filter((o) => selectedAgency && o.company_id === selectedAgency.company_id),
    [objects.data, selectedAgency],
  );

  function openCreate() {
    setEditing(null);
    setForm({ ...EMPTY, agency_id: filter.agency_id || "" });
    setOpen(true);
  }
  function openEdit(t: Task) {
    setEditing(t);
    setForm({
      agency_id: String(t.agency_id),
      object_catalog_id: String(t.object_catalog_id),
      extract_sql: t.extract_sql,
      schedule_seconds: String(t.schedule_seconds),
      is_active: t.is_active,
      run_on_company_token: t.run_on_company_token,
    });
    setOpen(true);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const sched = Number(form.schedule_seconds);
    if (!form.agency_id || !form.object_catalog_id) return toast.error("Selecciona agencia y objeto.");
    if (!Number.isInteger(sched) || sched < 10) return toast.error("La programación debe ser un entero ≥ 10 segundos.");
    if (!form.extract_sql.trim()) return toast.error("El SQL de extracción es obligatorio.");
    const body = {
      agency_id: Number(form.agency_id),
      object_catalog_id: Number(form.object_catalog_id),
      extract_sql: form.extract_sql,
      schedule_seconds: sched,
      is_active: form.is_active,
      run_on_company_token: form.run_on_company_token,
    };
    setSaving(true);
    const res = await run<Task>("save", editing ? `admin/tasks/${editing.id}` : "admin/tasks", {
      method: editing ? "PUT" : "POST",
      body,
      success: editing ? "Tarea actualizada." : "Tarea creada.",
    });
    setSaving(false);
    if (res) setOpen(false);
  }

  return (
    <>
      <PageHeader
        title="Tareas"
        description="Objetos del catálogo asignados a cada agencia, con su SQL de extracción y programación."
        actions={
          <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate} disabled={agencies.length === 0}>
            Nueva tarea
          </Button>
        }
      />
      <div className="mb-4">
        <HierarchyFilters value={filter} onChange={setFilter} groups={groups} companies={companies} agencies={agencies} />
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
              {items.map((t) => (
                <Tr key={t.id}>
                  <Td className="tabular-nums text-slate-500">{t.id}</Td>
                  <Td>
                    <p className="font-medium text-slate-900">{t.agency_name}</p>
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
                      hideLabel
                      label={t.is_active ? "Desactivar tarea" : "Activar tarea"}
                      onChange={(v) => run("toggle", `admin/tasks/${t.id}/${v ? "enable" : "disable"}`, { success: v ? "Tarea activada." : "Tarea desactivada." })}
                    />
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-0.5">
                      <IconButton label="Editar" onClick={() => openEdit(t)}>
                        <Pencil className="h-4 w-4" />
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
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </DataState>
      </Card>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        size="xl"
        title={editing ? `Editar tarea #${editing.id}` : "Nueva tarea"}
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button type="submit" form="task-form" loading={saving}>
              {editing ? "Guardar cambios" : "Crear tarea"}
            </Button>
          </>
        }
      >
        <form id="task-form" onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-6">
          <Field label="Agencia" required className="sm:col-span-3" htmlFor="t-agency">
            <Select
              id="t-agency"
              required
              value={form.agency_id}
              onChange={(e) => {
                const a = agencies.find((x) => String(x.id) === e.target.value);
                setForm((f) => {
                  const keepObject = (objects.data?.items ?? []).some((o) => String(o.id) === f.object_catalog_id && a && o.company_id === a.company_id);
                  return { ...f, agency_id: e.target.value, object_catalog_id: keepObject ? f.object_catalog_id : "" };
                });
              }}
            >
              <option value="" disabled>
                Selecciona…
              </option>
              <AgencyOptions agencies={agencies} />
            </Select>
          </Field>
          <Field
            label="Objeto del catálogo"
            required
            className="sm:col-span-3"
            htmlFor="t-object"
            hint={selectedAgency && objectOptions.length === 0 ? "La empresa de esta agencia no tiene objetos en el catálogo." : "Solo objetos de la empresa de la agencia."}
          >
            <Select id="t-object" required value={form.object_catalog_id} onChange={(e) => set("object_catalog_id", e.target.value)} disabled={!selectedAgency}>
              <option value="" disabled>
                {selectedAgency ? "Selecciona…" : "Primero elige la agencia"}
              </option>
              {objectOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name} → {o.destination_table}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="SQL de extracción"
            required
            className="sm:col-span-6"
            htmlFor="t-sql"
            hint={
              <>
                Se ejecuta en la BD de origen. Usa <code className="font-mono">{"'{last_run}'"}</code> para cargas incrementales (el cliente lo sustituye por la
                fecha de la última ejecución, o 1900-01-01 si nunca se ha ejecutado).
              </>
            }
          >
            <Textarea id="t-sql" mono rows={12} required value={form.extract_sql} onChange={(e) => set("extract_sql", e.target.value)} placeholder="SELECT * FROM vista WHERE fecha >= '{last_run}'" />
          </Field>
          <Field label="Ejecutar cada (segundos)" className="sm:col-span-2" htmlFor="t-sched" hint={`= ${fmtSeconds(Number(form.schedule_seconds) || 0)}`}>
            <Input id="t-sched" inputMode="numeric" value={form.schedule_seconds} onChange={(e) => set("schedule_seconds", e.target.value)} />
          </Field>
          <div className="flex flex-wrap items-center gap-1.5 sm:col-span-4 sm:pt-6">
            {PRESETS.map((p) => (
              <Button key={p.value} type="button" size="sm" variant={Number(form.schedule_seconds) === p.value ? "primary" : "secondary"} onClick={() => set("schedule_seconds", String(p.value))}>
                {p.label}
              </Button>
            ))}
          </div>
          <div className="grid gap-3 sm:col-span-6 sm:grid-cols-2">
            <Switch checked={form.is_active} onChange={(v) => set("is_active", v)} label="Tarea activa" />
            <Switch
              checked={form.run_on_company_token}
              onChange={(v) => set("run_on_company_token", v)}
              label="Incluir en modo empresa (/configs)"
              description="Si se desactiva, solo se entrega a clientes en modo agencia o grupo."
            />
          </div>
          {editing && (
            <p className="text-xs text-slate-500 sm:col-span-6">
              Última ejecución: {fmtDate(editing.last_run_at)} · Actualizada: {fmtDate(editing.updated_at)}
            </p>
          )}
        </form>
      </Modal>
    </>
  );
}
