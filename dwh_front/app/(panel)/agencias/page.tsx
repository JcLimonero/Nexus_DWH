"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "next/navigation";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { Agency, ListResponse } from "@/lib/types";
import { Button, Card, Field, IconButton, Input, PageHeader, Select, Switch } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { TokenField } from "@/components/ui/token";
import { CompanyOptions } from "@/components/filters";
import { useRefData } from "@/components/ref-data";
import { useActions } from "@/components/use-actions";
import { useToast } from "@/components/ui/feedback";
import { useSession } from "@/components/session";
import { FilterBar, useUrlFilters } from "@/components/scope-filters";

const FILTER_KEYS = ["group_id", "company_id"] as const;

interface FormState {
  company_id: string;
  name: string;
  is_enabled: boolean;
  generate_token: boolean;
}

export default function AgenciasPage() {
  const [filter, setFilter, clearFilter] = useUrlFilters(FILTER_KEYS);
  const { data, loading, error, reload } = useApi<ListResponse<Agency>>(`admin/agencies${qs(filter)}`);
  const { companies: allCompanies } = useRefData();
  const { can, canAny } = useSession();
  const companies = allCompanies.filter((c) => can("config.manage", c.group_id));
  const { run } = useActions(reload);
  const toast = useToast();
  const [editing, setEditing] = useState<Agency | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>({ company_id: "", name: "", is_enabled: true, generate_token: true });
  const [saving, setSaving] = useState(false);
  const items = data?.items ?? [];

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  function openCreate() {
    setEditing(null);
    setForm({ company_id: filter.company_id || "", name: "", is_enabled: true, generate_token: true });
    setOpen(true);
  }
  // ?nueva=1 (p. ej. desde el detalle de grupo, empresa sin agencias): abre el alta con la empresa del filtro.
  const params = useSearchParams();
  const autoOpened = useRef(false);
  useEffect(() => {
    if (autoOpened.current || params.get("nueva") !== "1" || companies.length === 0) return;
    autoOpened.current = true;
    openCreate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, companies.length]);

  function openEdit(a: Agency) {
    setEditing(a);
    setForm({ company_id: String(a.company_id), name: a.name, is_enabled: a.is_enabled, generate_token: false });
    setOpen(true);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!form.company_id) return toast.error("Selecciona una empresa.");
    const body: Record<string, unknown> = { company_id: Number(form.company_id), name: form.name.trim(), is_enabled: form.is_enabled };
    if (!editing) body.generate_token = form.generate_token;
    setSaving(true);
    const res = await run<Agency>("save", editing ? `admin/agencies/${editing.id}` : "admin/agencies", {
      method: editing ? "PUT" : "POST",
      body,
      success: editing ? "Agencia actualizada." : "Agencia creada.",
    });
    setSaving(false);
    if (res) setOpen(false);
  }

  return (
    <>
      <PageHeader
        title="Agencias"
        description="Sedes de cada empresa. El token de agencia permite un cliente ETL por sede."
        actions={
          canAny("config.manage") ? (
            <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate} disabled={companies.length === 0}>
              Nueva agencia
            </Button>
          ) : undefined
        }
      />
      <div className="mb-4">
        <FilterBar values={filter} onChange={setFilter} onClear={clearFilter} fields={[...FILTER_KEYS]} />
      </div>
      <Card>
        <DataState loading={loading} error={error} hasData={Boolean(data)} empty={items.length === 0} onRetry={reload} emptyTitle="No hay agencias" emptyDescription="Ajusta los filtros o crea una agencia.">
          <Table>
            <thead>
              <tr>
                <Th>Agencia</Th>
                <Th>Empresa / Grupo</Th>
                <Th>Token (x-agency-token)</Th>
                <Th className="text-right">Tareas</Th>
                <Th>Activa</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((a) => {
                const canCfg = can("config.manage", a.group_id);
                const canCred = can("credentials.manage", a.group_id);
                return (
                <Tr key={a.id}>
                  <Td>
                    <Link href={`/agencias/${a.id}`} className="font-medium text-slate-900 hover:text-brand-700 hover:underline">
                      {a.name}
                    </Link>
                  </Td>
                  <Td>
                    <p>{a.company_name}</p>
                    <p className="text-xs text-slate-500">{a.group_name}</p>
                  </Td>
                  <Td>
                    <TokenField
                      token={a.agency_token}
                      hidden={a.token_hidden}
                      emptyLabel="Sin token"
                      onRegenerate={!canCred ? undefined : () =>
                        run("token", `admin/agencies/${a.id}/regenerate-token`, {
                          success: a.agency_token ? "Token de agencia regenerado." : "Token de agencia generado.",
                          confirm: a.agency_token
                            ? {
                                title: "Regenerar token de agencia",
                                message: `El cliente ETL de "${a.name}" dejará de funcionar hasta que actualices su config.ini.`,
                                confirmLabel: "Regenerar",
                                danger: true,
                              }
                            : undefined,
                        })
                      }
                      onRevoke={
                        a.agency_token && canCred
                          ? () =>
                              run("token", `admin/agencies/${a.id}/token`, {
                                method: "DELETE",
                                success: "Token de agencia revocado.",
                                confirm: { title: "Revocar token de agencia", message: "El modo agencia dejará de funcionar para esta sede.", confirmLabel: "Revocar", danger: true },
                              })
                          : undefined
                      }
                    />
                  </Td>
                  <Td className="text-right tabular-nums">{a.task_count}</Td>
                  <Td>
                    <Switch
                      checked={a.is_enabled}
                      disabled={!canCfg}
                      hideLabel
                      label={a.is_enabled ? "Deshabilitar agencia" : "Habilitar agencia"}
                      onChange={(v) => run("toggle", `admin/agencies/${a.id}/${v ? "enable" : "disable"}`, { success: v ? "Agencia habilitada." : "Agencia deshabilitada." })}
                    />
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-0.5">
                      {!canCfg && <span className="text-xs text-slate-400">Solo lectura</span>}
                      {canCfg && (
                      <>
                      <IconButton label="Editar" onClick={() => openEdit(a)}>
                        <Pencil className="h-4 w-4" />
                      </IconButton>
                      <IconButton
                        label="Eliminar"
                        tone="danger"
                        onClick={() =>
                          run("delete", `admin/agencies/${a.id}`, {
                            method: "DELETE",
                            success: "Agencia eliminada.",
                            confirm: { title: "Eliminar agencia", message: `¿Eliminar "${a.name}"?`, confirmLabel: "Eliminar", danger: true },
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

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title={editing ? `Editar agencia: ${editing.name}` : "Nueva agencia"}
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button type="submit" form="agency-form" loading={saving}>
              {editing ? "Guardar cambios" : "Crear agencia"}
            </Button>
          </>
        }
      >
        <form id="agency-form" onSubmit={onSubmit} className="grid gap-4">
          <Field
            label="Empresa"
            required
            htmlFor="a-company"
            hint={editing && editing.task_count > 0 ? "No se puede cambiar de empresa mientras tenga tareas." : undefined}
          >
            <Select
              id="a-company"
              required
              value={form.company_id}
              onChange={(e) => set("company_id", e.target.value)}
              disabled={Boolean(editing && editing.task_count > 0)}
            >
              <option value="" disabled>
                Selecciona…
              </option>
              <CompanyOptions companies={companies} />
            </Select>
          </Field>
          <Field label="Nombre" required htmlFor="a-name">
            <Input id="a-name" required maxLength={255} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          {!editing && (
            <Switch checked={form.generate_token} onChange={(v) => set("generate_token", v)} label="Generar token de agencia" description="Necesario si esta sede tendrá su propio cliente ETL (modo agencia)." />
          )}
          <Switch checked={form.is_enabled} onChange={(v) => set("is_enabled", v)} label="Agencia habilitada" />
        </form>
      </Modal>
    </>
  );
}
