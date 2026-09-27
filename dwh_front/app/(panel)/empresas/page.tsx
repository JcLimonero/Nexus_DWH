"use client";

import { useState, type FormEvent } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { qs, useApi } from "@/lib/api";
import type { Company, EffectiveWarehouse, Group, ListResponse, SourceType } from "@/lib/types";
import { DEFAULT_PORTS, SOURCE_TYPE_LABELS } from "@/lib/format";
import { Badge, Button, Card, Field, IconButton, Input, PageHeader, Select, Switch } from "@/components/ui/primitives";
import { Modal } from "@/components/ui/modal";
import { DataState } from "@/components/ui/states";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { TokenField } from "@/components/ui/token";
import { PasswordInput, SecretNotice } from "@/components/password-input";
import { useActions } from "@/components/use-actions";
import { useToast } from "@/components/ui/feedback";
import { useSession } from "@/components/session";
import { FilterBar, useUrlFilters } from "@/components/scope-filters";
import {
  ConnectionTestPanel,
  DestinationChangeNotice,
  warehouseLocation,
  EMPTY_WAREHOUSE,
  EffectiveDestination,
  WarehouseFields,
  validateWarehouse,
  warehouseBody,
  type WarehouseForm,
} from "@/components/destination";

const FILTER_KEYS = ["group_id"] as const;

interface FormState {
  group_id: string;
  name: string;
  source_type: SourceType;
  source_host: string;
  source_port: string;
  source_database: string;
  source_username: string;
  source_password: string;
  clear_password: boolean;
  source_dsn: string;
  refresh_seconds: string;
  verbose_logging: boolean;
  is_enabled: boolean;
  /** true = usa el destino del grupo (predeterminado) */
  inherit_warehouse: boolean;
  warehouse: WarehouseForm;
}

const EMPTY: FormState = {
  group_id: "",
  name: "",
  source_type: "sqlserver",
  source_host: "",
  source_port: "1433",
  source_database: "",
  source_username: "",
  source_password: "",
  clear_password: false,
  source_dsn: "",
  refresh_seconds: "60",
  verbose_logging: false,
  is_enabled: true,
  inherit_warehouse: true,
  warehouse: EMPTY_WAREHOUSE,
};

function companyWarehouse(c: Company): WarehouseForm {
  if (c.warehouse_mode !== "custom") return EMPTY_WAREHOUSE;
  return {
    host: c.warehouse_host ?? "",
    port: String(c.warehouse_port),
    database: c.warehouse_database ?? "",
    username: c.warehouse_username ?? "",
    password: "",
    clear_password: false,
    schema: c.warehouse_schema || "public",
    sslmode: c.warehouse_sslmode || "prefer",
    sslrootcert: c.warehouse_sslrootcert ?? "",
  };
}

/** Cambios sin guardar en origen o destino (la prueba de conexión usa lo guardado). */
function connectionDirty(c: Company | null, f: FormState, kind: "source" | "dwh"): boolean {
  if (!c) return true;
  if (kind === "source") {
    return (
      c.source_type !== f.source_type || String(c.source_port) !== f.source_port || (c.source_host ?? "") !== f.source_host ||
      (c.source_database ?? "") !== f.source_database || (c.source_username ?? "") !== f.source_username ||
      (c.source_dsn ?? "") !== f.source_dsn || Boolean(f.source_password) || f.clear_password
    );
  }
  if ((c.warehouse_mode === "custom") === f.inherit_warehouse) return true;
  if (f.inherit_warehouse) return false;
  const saved = companyWarehouse(c);
  return (Object.keys(saved) as (keyof WarehouseForm)[]).some((k) => k !== "clear_password" && saved[k] !== f.warehouse[k]) || f.warehouse.clear_password;
}

const SECRET_TEXT_FIELDS = ["source_host", "source_database", "source_username", "source_dsn"] as const;

export default function EmpresasPage() {
  const [f, setF, clearF] = useUrlFilters(FILTER_KEYS);
  const groupFilter = f.group_id;
  const { data, loading, error, reload } = useApi<ListResponse<Company>>(`admin/companies${qs({ group_id: groupFilter })}`);
  const groups = useApi<ListResponse<Group>>("admin/groups");
  const { run } = useActions(reload);
  const toast = useToast();
  const { can, canAny } = useSession();
  const [editing, setEditing] = useState<Company | null>(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [resetSync, setResetSync] = useState(true);
  const items = data?.items ?? [];
  // Solo los grupos donde puede crear/mover empresas (config.manage).
  const groupList = (groups.data?.items ?? []).filter((g) => can("config.manage", g.id));
  const canCreate = canAny("config.manage") && groupList.length > 0;
  const formGroup = form.group_id ? Number(form.group_id) : null;
  const formCfg = editing ? can("config.manage", editing.group_id) : true;
  const formCred = editing ? can("credentials.manage", editing.group_id) : can("credentials.manage", formGroup);

  // ¿Cambia el destino efectivo (modo, ubicación del destino propio o grupo heredado)? → aviso de reinicio.
  const destinationChanged = Boolean(
    editing &&
      formCred &&
      !editing.secrets_hidden &&
      ((editing.warehouse_mode === "custom") === form.inherit_warehouse ||
        (!form.inherit_warehouse && warehouseLocation(companyWarehouse(editing)) !== warehouseLocation(form.warehouse)) ||
        (form.inherit_warehouse && Number(form.group_id) !== editing.group_id)),
  );

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }));

  function changeType(t: SourceType) {
    setForm((f) => {
      // Si el puerto sigue siendo el predeterminado del tipo anterior, se ajusta al nuevo.
      const wasDefault = String(DEFAULT_PORTS[f.source_type]) === f.source_port || !f.source_port;
      return { ...f, source_type: t, source_port: wasDefault ? String(DEFAULT_PORTS[t]) : f.source_port };
    });
  }

  function openCreate() {
    setEditing(null);
    setForm({ ...EMPTY, group_id: groupFilter || (groupList[0] ? String(groupList[0].id) : "") });
    setOpen(true);
  }
  function openEdit(c: Company) {
    setEditing(c);
    setForm({
      group_id: String(c.group_id),
      name: c.name,
      source_type: c.source_type,
      source_host: c.source_host ?? "",
      source_port: String(c.source_port),
      source_database: c.source_database ?? "",
      source_username: c.source_username ?? "",
      source_password: "",
      clear_password: false,
      source_dsn: c.source_dsn ?? "",
      refresh_seconds: String(c.refresh_seconds),
      verbose_logging: c.verbose_logging,
      is_enabled: c.is_enabled,
      inherit_warehouse: c.warehouse_mode !== "custom",
      warehouse: companyWarehouse(c),
    });
    setResetSync(true);
    setOpen(true);
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const port = Number(form.source_port);
    const refresh = Number(form.refresh_seconds);
    if (!form.group_id) return toast.error("Selecciona un grupo.");
    if (!Number.isInteger(port) || port < 1 || port > 65535) return toast.error("El puerto debe estar entre 1 y 65535.");
    if (!Number.isInteger(refresh) || refresh < 5 || refresh > 86400) return toast.error("El refresco debe estar entre 5 y 86400 segundos.");

    const body: Record<string, unknown> = {};
    if (formCfg) {
      Object.assign(body, {
        name: form.name.trim(),
        refresh_seconds: refresh,
        verbose_logging: form.verbose_logging,
        is_enabled: form.is_enabled,
      });
      if (!editing || Number(form.group_id) !== editing.group_id) body.group_id = Number(form.group_id);
    }
    if (formCred) {
      body.source_type = form.source_type;
      body.source_port = port;
      const undecryptable = editing?.decrypt_errors ?? [];
      SECRET_TEXT_FIELDS.forEach((k) => {
        if (undecryptable.includes(k) && !form[k]) return;
        if (!editing && !form[k].trim()) return;
        body[k] = form[k].trim();
      });
      if (form.source_password) body.source_password = form.source_password;
      if (editing && form.clear_password && !form.source_password) body.clear_password = true;
      // Destino: heredar el del grupo (predeterminado) o uno propio completo.
      if (form.inherit_warehouse) {
        if (!editing || editing.warehouse_mode === "custom") body.warehouse_mode = "inherit";
      } else {
        // Host/base/usuario no descifrables: se conservan si quedan vacíos (el backend valida el resultado).
        const undecryptable = editing?.decrypt_errors ?? [];
        const problem = validateWarehouse(form.warehouse, {
          requireConnection: !undecryptable.some((k) => k.startsWith("warehouse_")),
        });
        if (problem) {
          toast.error(problem);
          return;
        }
        body.warehouse_mode = "custom";
        Object.assign(
          body,
          warehouseBody(form.warehouse, {
            isEdit: Boolean(editing && editing.warehouse_mode === "custom"),
            hasPassword: Boolean(editing?.warehouse_has_password),
            undecryptable,
            clearKey: "warehouse_clear_password",
          }),
        );
      }
    }

    if (destinationChanged) body.reset_sync = resetSync;
    setSaving(true);
    const res = await run<Company>("save", editing ? `admin/companies/${editing.id}` : "admin/companies", {
      method: editing ? "PUT" : "POST",
      body,
      success: editing ? "Empresa actualizada." : "Empresa creada.",
    });
    setSaving(false);
    if (res) setOpen(false);
  }

  const usesDsn = form.source_type === "sqlserver" || form.source_type === "pervasive" || form.source_type === "firebird";
  // Destino heredado: el del grupo elegido en el formulario.
  const formGroupRow = (groups.data?.items ?? []).find((g) => g.id === formGroup) ?? null;
  const inheritedSummary: EffectiveWarehouse | null = formGroupRow
    ? {
        source: "group",
        host: formGroupRow.secrets_hidden ? null : formGroupRow.warehouse_host,
        port: formGroupRow.warehouse_port,
        database: formGroupRow.secrets_hidden ? null : formGroupRow.warehouse_database,
        schema: formGroupRow.warehouse_schema,
        sslmode: formGroupRow.warehouse_sslmode,
        configured: formGroupRow.secrets_hidden || Boolean(formGroupRow.warehouse_host),
      }
    : editing && editing.effective_warehouse.source === "group" && formGroup === editing.group_id
      ? editing.effective_warehouse
      : null;

  return (
    <>
      <PageHeader
        title="Empresas"
        description="Razones sociales: conexión a la BD de origen (DMS), destino (el del grupo o uno propio) y token del cliente ETL."
        actions={
          canCreate ? (
            <Button icon={<Plus className="h-4 w-4" />} onClick={openCreate}>
              Nueva empresa
            </Button>
          ) : undefined
        }
      />
      <div className="mb-4">
        <FilterBar values={f} onChange={setF} onClear={clearF} fields={[...FILTER_KEYS]} />
      </div>
      <Card>
        <DataState
          loading={loading}
          error={error}
          hasData={Boolean(data)}
          empty={items.length === 0}
          onRetry={reload}
          emptyTitle="No hay empresas"
          emptyDescription={groupList.length === 0 ? "Primero crea un grupo." : "Crea una empresa para asignarle agencias y tareas."}
        >
          <Table>
            <thead>
              <tr>
                <Th>Empresa</Th>
                <Th>Origen</Th>
                <Th>Destino (DWH)</Th>
                <Th>Token (x-token)</Th>
                <Th className="text-right">Agencias / Objetos</Th>
                <Th>Activa</Th>
                <Th className="text-right">Acciones</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((c) => {
                const canCfg = can("config.manage", c.group_id);
                const canCred = can("credentials.manage", c.group_id);
                return (
                <Tr key={c.id}>
                  <Td>
                    <p className="font-medium text-slate-900">{c.name}</p>
                    <p className="text-xs text-slate-500">
                      {c.group_name}
                      {!c.group_enabled && <span className="ml-1 text-amber-600">(grupo deshabilitado)</span>}
                    </p>
                  </Td>
                  <Td>
                    <Badge tone="blue">{SOURCE_TYPE_LABELS[c.source_type] ?? c.source_type}</Badge>
                    {c.secrets_hidden ? (
                      <p className="mt-1 text-xs italic text-slate-400" title="Requiere el permiso «Administrar credenciales» sobre el grupo">
                        Conexión oculta (credenciales)
                      </p>
                    ) : (
                      <>
                        <p className="mt-1 font-mono text-xs">
                          {c.source_dsn ? `DSN=${c.source_dsn}` : `${c.source_host || "—"}:${c.source_port}`}
                        </p>
                        <p className="text-xs text-slate-500">
                          {c.source_database || "—"} · {c.source_username || "—"} {c.has_password ? "" : "· sin contraseña"}
                        </p>
                      </>
                    )}
                  </Td>
                  <Td>
                    <EffectiveDestination eff={c.effective_warehouse} />
                  </Td>
                  <Td>
                    <TokenField
                      token={c.company_token}
                      hidden={c.token_hidden}
                      onRegenerate={!canCred ? undefined : () =>
                        run("token", `admin/companies/${c.id}/regenerate-token`, {
                          success: "Token de empresa regenerado.",
                          confirm: {
                            title: "Regenerar token de empresa",
                            message: `Los clientes ETL que usan el token actual de "${c.name}" dejarán de funcionar hasta que actualices su config.ini.`,
                            confirmLabel: "Regenerar",
                            danger: true,
                          },
                        })
                      }
                    />
                  </Td>
                  <Td className="text-right tabular-nums">
                    {c.agency_count} / {c.object_count}
                  </Td>
                  <Td>
                    <Switch
                      checked={c.is_enabled}
                      disabled={!canCfg}
                      hideLabel
                      label={c.is_enabled ? "Deshabilitar empresa" : "Habilitar empresa"}
                      onChange={(v) => run("toggle", `admin/companies/${c.id}/${v ? "enable" : "disable"}`, { success: v ? "Empresa habilitada." : "Empresa deshabilitada." })}
                    />
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-0.5">
                      {(canCfg || canCred) && (
                        <IconButton label="Editar" onClick={() => openEdit(c)}>
                          <Pencil className="h-4 w-4" />
                        </IconButton>
                      )}
                      {!canCfg && !canCred && <span className="text-xs text-slate-400">Solo lectura</span>}
                      {canCfg && (
                      <IconButton
                        label="Eliminar"
                        tone="danger"
                        onClick={() =>
                          run("delete", `admin/companies/${c.id}`, {
                            method: "DELETE",
                            success: "Empresa eliminada.",
                            confirm: { title: "Eliminar empresa", message: `¿Eliminar "${c.name}"? Esta acción no se puede deshacer.`, confirmLabel: "Eliminar", danger: true },
                          })
                        }
                      >
                        <Trash2 className="h-4 w-4" />
                      </IconButton>
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
        size="lg"
        title={editing ? `Editar empresa: ${editing.name}` : "Nueva empresa"}
        description="El token se genera automáticamente al crear la empresa. El destino es el del grupo salvo que configures uno propio."
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancelar
            </Button>
            <Button type="submit" form="company-form" loading={saving}>
              {editing ? "Guardar cambios" : "Crear empresa"}
            </Button>
          </>
        }
      >
        <form id="company-form" onSubmit={onSubmit} className="grid gap-4 sm:grid-cols-6">
          <Field label="Grupo" required className="sm:col-span-3" htmlFor="c-group">
            <Select id="c-group" required disabled={!formCfg} value={form.group_id} onChange={(e) => set("group_id", e.target.value)}>
              <option value="" disabled>
                Selecciona…
              </option>
              {editing && !groupList.some((g) => g.id === editing.group_id) && (
                <option value={editing.group_id}>{editing.group_name}</option>
              )}
              {groupList.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Nombre" required className="sm:col-span-3" htmlFor="c-name">
            <Input id="c-name" required maxLength={255} disabled={!formCfg} value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>

          <div className="sm:col-span-6">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Base de datos de origen</h3>
            {!formCred && (
              <p className="mt-1 text-xs text-slate-500">Sin permiso «Administrar credenciales» en este grupo: la conexión no se muestra ni se puede cambiar.</p>
            )}
          </div>
          <fieldset disabled={!formCred} className="contents">
          <Field label="Tipo" className="sm:col-span-2" htmlFor="c-type">
            <Select id="c-type" value={form.source_type} onChange={(e) => changeType(e.target.value as SourceType)}>
              {Object.entries(SOURCE_TYPE_LABELS).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Host / IP" className="sm:col-span-3" htmlFor="c-host">
            <Input id="c-host" value={form.source_host} onChange={(e) => set("source_host", e.target.value)} />
          </Field>
          <Field label="Puerto" className="sm:col-span-1" htmlFor="c-port">
            <Input id="c-port" inputMode="numeric" value={form.source_port} onChange={(e) => set("source_port", e.target.value)} />
          </Field>
          <Field label={form.source_type === "firebird" ? "Base de datos (ruta .fdb)" : "Base de datos"} className="sm:col-span-2" htmlFor="c-db">
            <Input id="c-db" value={form.source_database} onChange={(e) => set("source_database", e.target.value)} />
          </Field>
          <Field label="Usuario" className="sm:col-span-2" htmlFor="c-user">
            <Input id="c-user" autoComplete="off" value={form.source_username} onChange={(e) => set("source_username", e.target.value)} />
          </Field>
          <Field label="Contraseña" className="sm:col-span-2" htmlFor="c-pass" hint={editing ? "Vacía = conservar la actual." : undefined}>
            <PasswordInput id="c-pass" value={form.source_password} onChange={(v) => set("source_password", v)} hasPassword={Boolean(editing?.has_password)} isEdit={Boolean(editing)} />
          </Field>
          {usesDsn && (
            <Field label="DSN ODBC (opcional)" className="sm:col-span-6" htmlFor="c-dsn" hint="Si se indica, el cliente se conecta por DSN en lugar de host/puerto.">
              <Input id="c-dsn" value={form.source_dsn} onChange={(e) => set("source_dsn", e.target.value)} />
            </Field>
          )}
          {editing?.has_password && (
            <div className="sm:col-span-6">
              <Switch checked={form.clear_password} disabled={!formCred} onChange={(v) => set("clear_password", v)} label="Borrar la contraseña guardada" />
            </div>
          )}
          </fieldset>
          {editing && (
            <div className="sm:col-span-6">
              <ConnectionTestPanel
                targetKind="company_source"
                companyId={editing.id}
                title="Probar conexión al origen"
                canRun={can("config.manage", editing.group_id)}
                disabledReason={formCred && connectionDirty(editing, form, "source") ? "Hay cambios sin guardar en el origen: la prueba usa la configuración guardada." : null}
              />
            </div>
          )}

          <div className="sm:col-span-6">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Destino (DWH)</h3>
            <p className="mt-1 text-xs text-slate-500">
              De forma predeterminada la empresa carga en el destino de su grupo. Desactiva la opción para usar un destino propio (otro servidor, base, esquema o SSL).
            </p>
          </div>
          <div className="sm:col-span-6">
            <Switch
              checked={form.inherit_warehouse}
              disabled={!formCred}
              onChange={(v) => set("inherit_warehouse", v)}
              label="Usar el destino del grupo"
              description={
                form.inherit_warehouse
                  ? "Recomendado. Los cambios en el destino del grupo aplican automáticamente a esta empresa."
                  : "Al volver a activarla se descarta el destino propio guardado."
              }
            />
          </div>
          {form.inherit_warehouse ? (
            <div className="sm:col-span-6 rounded-md border border-slate-200 px-3 py-2">
              {inheritedSummary ? (
                <EffectiveDestination eff={inheritedSummary} />
              ) : (
                <p className="text-xs text-slate-500">Selecciona un grupo para ver su destino.</p>
              )}
            </div>
          ) : (
            <WarehouseFields
              idPrefix="c-wh"
              value={form.warehouse}
              onChange={(w) => set("warehouse", w)}
              disabled={!formCred}
              isEdit={Boolean(editing && editing.warehouse_mode === "custom")}
              hasPassword={Boolean(editing?.warehouse_has_password)}
              hasCa={editing?.warehouse_has_sslrootcert}
            />
          )}
          {editing && destinationChanged && (
            <div className="sm:col-span-6">
              <DestinationChangeNotice tasks={editing.task_count} reset={resetSync} onReset={setResetSync} />
            </div>
          )}
          {editing && (
            <div className="sm:col-span-6">
              <ConnectionTestPanel
                targetKind="company_dwh"
                companyId={editing.id}
                title={editing.warehouse_mode === "custom" ? "Probar conexión al destino propio" : "Probar conexión al destino (del grupo)"}
                canRun={can("config.manage", editing.group_id)}
                disabledReason={formCred && connectionDirty(editing, form, "dwh") ? "Hay cambios sin guardar en el destino: la prueba usa la configuración guardada." : null}
              />
            </div>
          )}
          <fieldset disabled={!formCfg} className="contents">

          <div className="sm:col-span-6">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Cliente ETL</h3>
          </div>
          <Field label="Refresco de configuración (s)" className="sm:col-span-2" htmlFor="c-refresh">
            <Input id="c-refresh" inputMode="numeric" value={form.refresh_seconds} onChange={(e) => set("refresh_seconds", e.target.value)} />
          </Field>
          <div className="flex flex-col justify-end gap-3 sm:col-span-4">
            <Switch checked={form.verbose_logging} disabled={!formCfg} onChange={(v) => set("verbose_logging", v)} label="Log detallado (verbose)" />
            <Switch checked={form.is_enabled} disabled={!formCfg} onChange={(v) => set("is_enabled", v)} label="Empresa habilitada" />
          </div>
          </fieldset>
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
