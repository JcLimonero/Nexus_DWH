"use client";

import { useState, type FormEvent } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { CatalogObject, ListResponse } from "@/lib/types";
import { Badge, Button, Card, Field, IconButton, Input, PageHeader, Select, Switch, Textarea } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { CompanyOptions, HierarchyFilters, type FilterValue } from "@/components/filters";
import { useRefData } from "@/components/ref-data";
import { useActions } from "@/components/use-actions";
import { useToast } from "@/components/ui/feedback";

interface FormState {
  company_id: string;
  name: string;
  description: string;
  destination_table: string;
  upsert_keys: string;
  create_table_sql: string;
  constraint_name: string;
  create_constraint_sql: string;
  static_columns: string;
  is_enabled: boolean;
}

const EMPTY: FormState = {
  company_id: "",
  name: "",
  description: "",
  destination_table: "",
  upsert_keys: "",
  create_table_sql: "",
  constraint_name: "",
  create_constraint_sql: "",
  static_columns: "",
  is_enabled: true,
};

export default function CatalogoPage() {
  const [filter, setFilter] = useState<FilterValue>({ group_id: "", company_id: "" });
  const { data, loading, error, reload } = useApi<ListResponse<CatalogObject>>(`admin/objects${qs(filter as unknown as Record<string, string>)}`);
  const { groups, companies } = useRefData();
  const { run } = useActions(reload);
  const toast = useToast();
  const [editing, setEditing] = useState<CatalogObject | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const items = data?.items ?? [];

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  function openCreate() {
    setEditing(null);
    setForm({ ...EMPTY, company_id: filter.company_id || "" });
    setOpen(true);
  }
  function openEdit(o: CatalogObject) {
    setEditing(o);
    setForm({
      company_id: String(o.company_id),
      name: o.name,
      description: o.description ?? "",
      destination_table: o.destination_table,
      upsert_keys: o.upsert_keys ?? "",
      create_table_sql: o.create_table_sql ?? "",
      constraint_name: o.constraint_name ?? "",
      create_constraint_sql: o.create_constraint_sql ?? "",
      static_columns: o.static_columns ?? "",
      is_enabled: o.is_enabled,
    });
    setOpen(true);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!form.company_id) return toast.error("Selecciona una empresa.");
    const body = {
      company_id: Number(form.company_id),
      name: form.name.trim(),
      description: form.description,
      destination_table: form.destination_table.trim(),
      upsert_keys: form.upsert_keys,
      create_table_sql: form.create_table_sql,
      constraint_name: form.constraint_name.trim(),
      create_constraint_sql: form.create_constraint_sql,
      static_columns: form.static_columns,
      is_enabled: form.is_enabled,
    };
    setSaving(true);
    const res = await run<CatalogObject>("save", editing ? `admin/objects/${editing.id}` : "admin/objects", {
      method: editing ? "PUT" : "POST",
      body,
      success: editing ? "Objeto actualizado." : "Objeto creado.",
    });
    setSaving(false);
    if (res) setOpen(false);
  }

  return (
    <>
      <PageHeader
        title="Catálogo de objetos"
        description="Plantillas de tablas destino en el DWH (DDL, claves de upsert) por empresa."
        actions={
          <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate} disabled={companies.length === 0}>
            Nuevo objeto
          </Button>
        }
      />
      <div className="mb-4">
        <HierarchyFilters value={filter} onChange={setFilter} groups={groups} companies={companies} />
      </div>
      <Card>
        <DataState loading={loading} error={error} hasData={Boolean(data)} empty={items.length === 0} onRetry={reload} emptyTitle="No hay objetos en el catálogo">
          <Table>
            <thead>
              <tr>
                <Th>Objeto</Th>
                <Th>Empresa</Th>
                <Th>Tabla destino</Th>
                <Th>Claves upsert</Th>
                <Th className="text-right">Tareas</Th>
                <Th>Activo</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((o) => (
                <Tr key={o.id}>
                  <Td>
                    <p className="font-medium text-slate-900">{o.name}</p>
                    {o.description && <p className="max-w-xs truncate text-xs text-slate-500">{o.description}</p>}
                  </Td>
                  <Td>
                    <p>{o.company_name}</p>
                    <p className="text-xs text-slate-500">{o.group_name}</p>
                  </Td>
                  <Td>
                    <code className="font-mono text-xs">{o.destination_table}</code>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {o.create_table_sql && <Badge tone="blue">DDL</Badge>}
                      {o.create_constraint_sql && <Badge tone="blue">Constraint</Badge>}
                      {o.static_columns && <Badge>Cols. estáticas</Badge>}
                    </div>
                  </Td>
                  <Td>
                    <code className="font-mono text-xs text-slate-600">{o.upsert_keys || "—"}</code>
                  </Td>
                  <Td className="text-right tabular-nums">{o.task_count}</Td>
                  <Td>
                    <Switch
                      checked={o.is_enabled}
                      hideLabel
                      label={o.is_enabled ? "Deshabilitar objeto" : "Habilitar objeto"}
                      onChange={(v) => run("toggle", `admin/objects/${o.id}/${v ? "enable" : "disable"}`, { success: v ? "Objeto habilitado." : "Objeto deshabilitado." })}
                    />
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-0.5">
                      <IconButton label="Editar" onClick={() => openEdit(o)}>
                        <Pencil className="h-4 w-4" />
                      </IconButton>
                      <IconButton
                        label="Eliminar"
                        tone="danger"
                        onClick={() =>
                          run("delete", `admin/objects/${o.id}`, {
                            method: "DELETE",
                            success: "Objeto eliminado.",
                            confirm: { title: "Eliminar objeto", message: `¿Eliminar "${o.name}" del catálogo?`, confirmLabel: "Eliminar", danger: true },
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
        title={editing ? `Editar objeto: ${editing.name}` : "Nuevo objeto"}
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button type="submit" form="object-form" loading={saving}>
              {editing ? "Guardar cambios" : "Crear objeto"}
            </Button>
          </>
        }
      >
        <form id="object-form" onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-6">
          <Field label="Empresa" required className="sm:col-span-3" htmlFor="o-company" hint={editing && editing.task_count > 0 ? "No se puede cambiar mientras tenga tareas." : undefined}>
            <Select id="o-company" required value={form.company_id} onChange={(e) => set("company_id", e.target.value)} disabled={Boolean(editing && editing.task_count > 0)}>
              <option value="" disabled>
                Selecciona…
              </option>
              <CompanyOptions companies={companies} />
            </Select>
          </Field>
          <Field label="Nombre" required className="sm:col-span-3" htmlFor="o-name">
            <Input id="o-name" required maxLength={255} value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Inventory" />
          </Field>
          <Field label="Descripción" className="sm:col-span-6" htmlFor="o-desc">
            <Input id="o-desc" value={form.description} onChange={(e) => set("description", e.target.value)} />
          </Field>
          <Field label="Tabla destino" required className="sm:col-span-3" htmlFor="o-table" hint="Identificador SQL (opcional esquema.tabla).">
            <Input id="o-table" required className="font-mono" value={form.destination_table} onChange={(e) => set("destination_table", e.target.value)} placeholder="inventory" />
          </Field>
          <Field label="Claves de upsert" className="sm:col-span-3" htmlFor="o-keys" hint="Separadas por coma o una por línea.">
            <Textarea id="o-keys" mono rows={2} value={form.upsert_keys} onChange={(e) => set("upsert_keys", e.target.value)} placeholder="idAgency, vin" />
          </Field>
          <Field label="SQL de creación de tabla (create_table_sql)" className="sm:col-span-6" htmlFor="o-ddl" hint="DDL PostgreSQL que el cliente ejecuta en el DWH (usa CREATE TABLE IF NOT EXISTS).">
            <Textarea id="o-ddl" mono rows={9} value={form.create_table_sql} onChange={(e) => set("create_table_sql", e.target.value)} />
          </Field>
          <Field label="Nombre de constraint" className="sm:col-span-2" htmlFor="o-cname">
            <Input id="o-cname" className="font-mono" value={form.constraint_name} onChange={(e) => set("constraint_name", e.target.value)} />
          </Field>
          <Field label="SQL de constraint" className="sm:col-span-4" htmlFor="o-csql">
            <Textarea id="o-csql" mono rows={3} value={form.create_constraint_sql} onChange={(e) => set("create_constraint_sql", e.target.value)} />
          </Field>
          <Field label="Columnas estáticas (static_columns)" className="sm:col-span-6" htmlFor="o-static">
            <Textarea id="o-static" mono rows={2} value={form.static_columns} onChange={(e) => set("static_columns", e.target.value)} />
          </Field>
          <div className="sm:col-span-6">
            <Switch checked={form.is_enabled} onChange={(v) => set("is_enabled", v)} label="Objeto habilitado" description="Si se deshabilita, sus tareas no se entregan a los clientes." />
          </div>
        </form>
      </Modal>
    </>
  );
}
