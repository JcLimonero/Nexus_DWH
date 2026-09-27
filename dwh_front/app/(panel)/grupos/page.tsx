"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useApi } from "@/lib/api";
import type { Group, ListResponse } from "@/lib/types";
import { Button, Card, Field, IconButton, Input, PageHeader, Switch } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { TokenField } from "@/components/ui/token";
import { SecretNotice } from "@/components/password-input";
import {
  ConnectionTestPanel,
  DestinationChangeNotice,
  warehouseLocation,
  EMPTY_WAREHOUSE,
  WarehouseFields,
  sslLabel,
  validateWarehouse,
  warehouseBody,
  type WarehouseForm,
} from "@/components/destination";
import { useActions } from "@/components/use-actions";
import { useToast } from "@/components/ui/feedback";
import { useSession } from "@/components/session";

interface FormState {
  name: string;
  warehouse: WarehouseForm;
  is_enabled: boolean;
}

const EMPTY: FormState = {
  name: "",
  warehouse: EMPTY_WAREHOUSE,
  is_enabled: true,
};

function warehouseOf(g: Group): WarehouseForm {
  return {
    host: g.warehouse_host ?? "",
    port: String(g.warehouse_port),
    database: g.warehouse_database ?? "",
    username: g.warehouse_username ?? "",
    password: "",
    clear_password: false,
    schema: g.warehouse_schema || "public",
    sslmode: g.warehouse_sslmode || "prefer",
    sslrootcert: g.warehouse_sslrootcert ?? "",
  };
}

/** ¿El formulario tiene cambios de conexión sin guardar? (la prueba usa lo guardado) */
function warehouseDirty(g: Group | null, w: WarehouseForm): boolean {
  if (!g) return true;
  const saved = warehouseOf(g);
  return (Object.keys(saved) as (keyof WarehouseForm)[]).some((k) => k !== "clear_password" && saved[k] !== w[k]) || w.clear_password;
}

export default function GruposPage() {
  const { data, loading, error, reload } = useApi<ListResponse<Group>>("admin/groups");
  const { run } = useActions(reload);
  const toast = useToast();
  const { can, canGlobal } = useSession();
  const canCreate = canGlobal("config.manage");
  const [editing, setEditing] = useState<Group | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [resetSync, setResetSync] = useState(true);
  const items = data?.items ?? [];
  // En el formulario: configuración (nombre/activo) y credenciales del DWH van por permisos distintos.
  const formCfg = editing ? can("config.manage", editing.id) : canCreate;
  const formCred = editing ? can("credentials.manage", editing.id) : canGlobal("credentials.manage");

  // ¿Cambia la ubicación física del destino (host/puerto/base/esquema)? → aviso de reinicio de carga.
  const locationChanged = Boolean(editing && formCred && !editing.secrets_hidden && warehouseLocation(warehouseOf(editing)) !== warehouseLocation(form.warehouse));

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  function openCreate() {
    setEditing(null);
    setForm(EMPTY);
    setOpen(true);
  }
  function openEdit(g: Group) {
    setEditing(g);
    setForm({ name: g.name, warehouse: warehouseOf(g), is_enabled: g.is_enabled });
    setResetSync(true);
    setOpen(true);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    if (formCfg) {
      body.name = form.name.trim();
      body.is_enabled = form.is_enabled;
    }
    if (formCred) {
      const problem = validateWarehouse(form.warehouse);
      if (problem) {
        toast.error(problem);
        return;
      }
      Object.assign(
        body,
        warehouseBody(form.warehouse, {
          isEdit: Boolean(editing),
          hasPassword: Boolean(editing?.has_password),
          undecryptable: editing?.decrypt_errors,
        }),
      );
      if (locationChanged) body.reset_sync = resetSync;
    }
    if (!editing) body.name = form.name.trim();

    setSaving(true);
    const res = await run<Group>("save", editing ? `admin/groups/${editing.id}` : "admin/groups", {
      method: editing ? "PUT" : "POST",
      body,
      success: editing ? "Grupo actualizado." : "Grupo creado.",
    });
    setSaving(false);
    if (res) setOpen(false);
  }

  return (
    <>
      <PageHeader
        title="Grupos"
        description="Grupos de empresas y su conexión al Data Warehouse."
        actions={
          canCreate ? (
            <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate}>
              Nuevo grupo
            </Button>
          ) : undefined
        }
      />
      <Card>
        <DataState
          loading={loading}
          error={error}
          hasData={Boolean(data)}
          empty={items.length === 0}
          onRetry={reload}
          emptyTitle="No hay grupos"
          emptyDescription="Crea el primer grupo para empezar a configurar empresas."
          emptyAction={canCreate ? <Button size="sm" onClick={openCreate}>Nuevo grupo</Button> : undefined}
        >
          <Table>
            <thead>
              <tr>
                <Th>Nombre</Th>
                <Th>DWH</Th>
                <Th>Token de grupo</Th>
                <Th className="hidden text-right xl:table-cell">Empresas</Th>
                <Th>Activo</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((g) => {
                const canCfg = can("config.manage", g.id);
                const canCred = can("credentials.manage", g.id);
                return (
                <Tr key={g.id}>
                  <Td>
                    <Link href={`/grupos/${g.id}`} className="font-medium text-slate-900 hover:text-brand-700 hover:underline">
                      {g.name}
                    </Link>
                    <p className="text-xs text-slate-500 xl:hidden">{g.company_count} empresa(s)</p>
                  </Td>
                  <Td>
                    {g.secrets_hidden ? (
                      <p className="text-xs italic text-slate-400" title="Requiere el permiso «Administrar credenciales» sobre el grupo">
                        Conexión oculta (credenciales)
                      </p>
                    ) : (
                      <>
                        <p className="font-mono text-xs">
                          {g.warehouse_host || "—"}:{g.warehouse_port}
                        </p>
                        <p className="text-xs text-slate-500">
                          {g.warehouse_database || "—"} · {g.warehouse_username || "—"} {g.has_password ? "" : "· sin contraseña"}
                        </p>
                      </>
                    )}
                    <p className="text-xs text-slate-500">
                      esquema <span className="font-mono text-slate-700">{g.warehouse_schema}</span> · ssl {sslLabel(g.warehouse_sslmode)}
                      {g.custom_destination_count > 0 && (
                        <span className="ml-1 text-brand-700" title="Empresas del grupo que usan un destino propio">
                          · {g.custom_destination_count} empresa(s) con destino propio
                        </span>
                      )}
                    </p>
                  </Td>
                  <Td>
                    <TokenField
                      token={g.group_token}
                      hidden={g.token_hidden}
                      onRegenerate={!canCred ? undefined : () =>
                        run("token", `admin/groups/${g.id}/regenerate-token`, {
                          success: "Token de grupo regenerado.",
                          confirm: g.group_token
                            ? {
                                title: "Regenerar token de grupo",
                                message: `Los clientes ETL que usan el token actual de "${g.name}" dejarán de funcionar hasta que actualices su config.ini.`,
                                confirmLabel: "Regenerar",
                                danger: true,
                              }
                            : undefined,
                        })
                      }
                      onRevoke={
                        g.group_token && canCred
                          ? () =>
                              run("token", `admin/groups/${g.id}/token`, {
                                method: "DELETE",
                                success: "Token de grupo revocado.",
                                confirm: { title: "Revocar token de grupo", message: "El modo grupo dejará de funcionar para este grupo.", confirmLabel: "Revocar", danger: true },
                              })
                          : undefined
                      }
                    />
                  </Td>
                  <Td className="hidden text-right tabular-nums xl:table-cell">{g.company_count}</Td>
                  <Td>
                    <Switch
                      checked={g.is_enabled}
                      disabled={!canCfg}
                      hideLabel
                      label={g.is_enabled ? "Deshabilitar grupo" : "Habilitar grupo"}
                      onChange={(v) => run("toggle", `admin/groups/${g.id}/${v ? "enable" : "disable"}`, { success: v ? "Grupo habilitado." : "Grupo deshabilitado." })}
                    />
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-0.5">
                      {(canCfg || canCred) && (
                        <IconButton label="Editar" onClick={() => openEdit(g)}>
                          <Pencil className="h-4 w-4" />
                        </IconButton>
                      )}
                      {canCreate && (
                      <IconButton
                        label="Eliminar"
                        tone="danger"
                        onClick={() =>
                          run("delete", `admin/groups/${g.id}`, {
                            method: "DELETE",
                            success: "Grupo eliminado.",
                            confirm: { title: "Eliminar grupo", message: `¿Eliminar el grupo "${g.name}"? Esta acción no se puede deshacer.`, confirmLabel: "Eliminar", danger: true },
                          })
                        }
                      >
                        <Trash2 className="h-4 w-4" />
                      </IconButton>
                      )}
                      {!canCfg && !canCred && <span className="text-xs text-slate-400">Solo lectura</span>}
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
        size="lg"
        title={editing ? `Editar grupo: ${editing.name}` : "Nuevo grupo"}
        description="Destino (DWH) predeterminado de todas las empresas del grupo; cada empresa puede usar uno propio."
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button type="submit" form="group-form" loading={saving}>
              {editing ? "Guardar cambios" : "Crear grupo"}
            </Button>
          </>
        }
      >
        <form id="group-form" onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-6">
          <Field label="Nombre" required className="sm:col-span-6" htmlFor="g-name">
            <Input id="g-name" required maxLength={255} disabled={!formCfg} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <div className="sm:col-span-6">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Destino: Data Warehouse (PostgreSQL)</h3>
            {!formCred && (
              <p className="mt-1 text-xs text-slate-500">Sin permiso «Administrar credenciales»: la conexión no se muestra ni se puede cambiar.</p>
            )}
          </div>
          <WarehouseFields
            idPrefix="g"
            value={form.warehouse}
            onChange={(w) => set("warehouse", w)}
            disabled={!formCred}
            isEdit={Boolean(editing)}
            hasPassword={Boolean(editing?.has_password)}
            hasCa={editing?.has_sslrootcert}
          />
          {editing && locationChanged && (
            <div className="sm:col-span-6">
              <DestinationChangeNotice tasks={editing.inherited_task_count} reset={resetSync} onReset={setResetSync} />
            </div>
          )}
          {editing && (
            <div className="sm:col-span-6">
              <ConnectionTestPanel
                targetKind="group_dwh"
                groupId={editing.id}
                canRun={can("config.manage", editing.id)}
                disabledReason={formCred && warehouseDirty(editing, form.warehouse) ? "Hay cambios sin guardar: la prueba usa la configuración guardada." : null}
              />
            </div>
          )}
          <div className="sm:col-span-6">
            <Switch checked={form.is_enabled} disabled={!formCfg} onChange={(v) => set("is_enabled", v)} label="Grupo habilitado" description="Si se deshabilita, ningún cliente del grupo recibe configuración." />
          </div>
          {editing && (
            <div className="sm:col-span-6">
              <SecretNotice encrypted={editing.encrypted_fields} errors={editing.decrypt_errors} />
            </div>
          )}
        </form>
      </Modal>
    </>
  );
}
